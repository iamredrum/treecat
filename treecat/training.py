from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
import logging

import numpy as np
from scipy.special import gammaln

from treecat.structure import TreeStructure
from treecat.structure import make_propagation_schedule
from treecat.structure import sample_tree
from treecat.util import art_logger
from treecat.util import profile
from treecat.util import sample_from_probs

logger = logging.getLogger(__name__)


def count_pairs(assignments, v1, v2, M):
    """Construct sufficient statistics for (v1, v2) pairs.

    Args:
      assignments: An _ x V assignment matrix with values in range(M).
      v1, v2: Column ids of the assignments matrix.
      M: The number of possible assignment bins.

    Returns:
      And M x M array of counts.
    """
    assert v1 != v2
    pairs = assignments[:, v1].astype(np.int32) * M + assignments[:, v2]
    return np.bincount(pairs, minlength=M * M).reshape((M, M))


def logprob_dc(counts_plus_prior, axis=None):
    """Non-normalized log probability of a Dirichlet-Categorical distribution.

    See https://en.wikipedia.org/wiki/Dirichlet-multinomial_distribution
    """
    return gammaln(counts_plus_prior).sum(axis)


class TreeCatTrainer(object):
    """Class for training a TreeCat model."""

    def __init__(self, data, config):
        """Initialize a model in an unassigned state.

        Args:
          data: A list of numpy arrays, where each array is an N x _ column of
            counts of multinomial data.
          config: A global config dict.
        """
        logger.info('TreeCatTrainer of %d x %d data', data[0].shape[0],
                    len(data))
        data = [np.asarray(column, np.int8) for column in data]
        num_features = len(data)
        num_rows = len(data[0])
        assert all(column.shape[0] == num_rows for column in data)
        self._data = data
        self._config = config
        self._assigned_rows = set()
        self.assignments = np.zeros([num_rows, num_features], dtype=np.int8)
        self.tree = TreeStructure(num_features)
        self._schedule = make_propagation_schedule(self.tree.tree_grid)

        # These are useful dimensions to import into locals().
        V = self.tree.num_vertices
        E = V - 1  # Number of edges in the tree.
        K = V * (V - 1) // 2  # Number of edges in the complete graph.
        M = self._config['model_num_clusters']  # Clusters per latent.
        assert M <= 128, 'Invalid model_num_clusters > 128: {}'.format(M)
        self._VEKM = (V, E, K, M)

        # Use Jeffreys priors.
        self._vert_prior = 0.5
        self._edge_prior = 0.5 / M
        self._feat_prior = 0.5 / M

        # Sufficient statistics are maintained always.
        self._vert_ss = np.zeros([V, M], np.int32)
        self._edge_ss = np.zeros([E, M, M], np.int32)
        self._feat_ss = [
            np.zeros([column.shape[1], M], np.int32) for column in data
        ]

    @property
    def suffstats(self):
        return {
            'feat_ss': self._feat_ss,
            'vert_ss': self._vert_ss,
            'edge_ss': self._edge_ss,
        }

    def _update_tree(self):
        V, E, K, M = self._VEKM
        assignments = self.assignments[sorted(self._assigned_rows), :]
        for e, v1, v2 in self.tree.tree_grid.T:
            self._edge_ss[e, :, :] = count_pairs(assignments, v1, v2, M)
        self._schedule = make_propagation_schedule(self.tree.tree_grid)

    def _update_tensors(self, row_id, diff):
        assignments = self.assignments[row_id, :]
        self._vert_ss[self.tree.vertices, assignments] += diff
        self._edge_ss[self.tree.tree_grid[0, :],  #
                      assignments[self.tree.tree_grid[1, :]],  #
                      assignments[self.tree.tree_grid[2, :]]] += diff
        for v, column in enumerate(self._data):
            self._feat_ss[v][:, assignments[v]] += diff * column[row_id, :]

    @profile
    def add_row(self, row_id):
        logger.debug('TreeCatTrainer.add_row %d', row_id)
        assert row_id not in self._assigned_rows, row_id
        assignments = self.assignments[row_id, :]
        edge_probs = self._edge_ss.astype(np.float32) + self._edge_prior
        vert_probs = self._vert_ss.astype(np.float32) + self._vert_prior
        messages = vert_probs.copy()

        for v, parent, children in reversed(self._schedule):
            message = messages[v, :]
            # Propagate upward from observed to latent.
            obs_lat = self._feat_ss[v].astype(np.float32) + self._feat_prior
            lat = obs_lat.sum(axis=0)
            for c, count in enumerate(self._data[v][row_id, :]):
                for _ in range(count):
                    message *= obs_lat[c, :] / lat
                    obs_lat[c, :] += 1.0
                    lat += 1.0
            # Propagate latent state inward from children to v.
            for child in children:
                e = self.tree.find_tree_edge[v, child]
                trans = edge_probs[e, :, :]
                if v > child:
                    trans = trans.T
                message *= np.dot(trans,
                                  messages[child, :] / vert_probs[child, :])
                message /= vert_probs[v, :]
                message /= message.sum()  # For numerical stability only.

        # Propagate latent state outward from parent to v.
        for v, parent, children in self._schedule:
            message = messages[v, :]
            if parent is not None:
                e = self.tree.find_tree_edge[parent, v]
                trans = edge_probs[e, :, :]
                if parent > v:
                    trans = trans.T
                message *= trans[assignments[parent], :]
                message /= vert_probs[v, :]
            message /= message.sum()
            assignments[v] = sample_from_probs(message)
        self._assigned_rows.add(row_id)
        self._update_tensors(row_id, +1)

    @profile
    def remove_row(self, row_id):
        logger.debug('TreeCatTrainer.remove_row %d', row_id)
        assert row_id in self._assigned_rows, row_id
        self._assigned_rows.remove(row_id)
        self._update_tensors(row_id, -1)

    @profile
    def sample_tree(self):
        logger.info('TreeCatTrainer.sample_tree given %d rows',
                    len(self._assigned_rows))
        V, E, K, M = self._VEKM
        assignments = self.assignments[sorted(self._assigned_rows), :]
        vertex_logits = logprob_dc(self._vert_ss + self._vert_prior, axis=1)
        edge_logits = np.zeros([K], np.float32)
        for k, v1, v2 in self.tree.tree_grid.T:
            counts = count_pairs(assignments, v1, v2, M)
            # This is the most expensive part of tree sampling:
            edge_logits[k] = (logprob_dc(counts + self._edge_prior) -
                              vertex_logits[v1] - vertex_logits[v2])

        # Sample the tree.
        complete_grid = self.tree.complete_grid
        assert edge_logits.shape[0] == complete_grid.shape[1]
        edges = [tuple(edge) for edge in self.tree.tree_grid[1:3, :].T]
        edges = sample_tree(
            complete_grid,
            edge_logits,
            edges,
            steps=self._config['learning_sample_tree_steps'])
        self.tree.set_edges(edges)
        self._update_tree()

    def logprob(self):
        """Compute non-normalized log probability of data and assignments.

        This is mainly useful for testing goodness of fit of the category
        kernel.
        """
        assert len(self._assigned_rows) == self.assignments.shape[0]
        V, E, K, M = self._VEKM
        vertex_logits = logprob_dc(self._vert_ss + self._vert_prior, axis=1)
        logprob = vertex_logits.sum()
        for e, v1, v2 in self.tree.tree_grid.T:
            logprob += (logprob_dc(self._edge_ss[e, :, :] + self._edge_prior) -
                        vertex_logits[v1] - vertex_logits[v2])
        for v in range(V):
            feat_probs = self._feat_ss[v] + self._feat_prior
            logprob += logprob_dc(feat_probs) - logprob_dc(feat_probs.sum(0))
        return logprob

    def finish(self):
        logger.info('TreeCatTrainer.finish with %d rows',
                    len(self._assigned_rows))
        self.tree.gc()

    def train(self):
        """Train a TreeCat model using subsample-annealed MCMC.

        Let N be the number of data rows and V be the number of features.

        Returns:
          A trained model as a dictionary with keys:
            tree: A TreeStructure instance with the learned latent structure.
            suffstats: Sufficient statistics of features, vertices, and
              edges.
            assignments: An [N, V] numpy array of latent cluster ids for each
              cell in the dataset.
        """
        logger.info('train()')
        np.random.seed(self._config['seed'])
        num_rows = self.assignments.shape[0]
        for action, row_id in get_annealing_schedule(num_rows, self._config):
            if action == 'add_row':
                art_logger('+')
                self.add_row(row_id)
            elif action == 'remove_row':
                art_logger('-')
                self.remove_row(row_id)
            else:
                art_logger('\n')
                self.sample_tree()
        self.finish()
        return {
            'config': self._config,
            'tree': self.tree,
            'suffstats': self.suffstats,
            'assignments': self.assignments,
        }


def train_model(data, config):
    """Train a TreeCat model using subsample-annealed MCMC.

    Let N be the number of data rows and V be the number of features.

    Args:
      data: A list of numpy arrays, where each array is an N x _ column of
        counts of multinomial data.
      config: A global config dict.

    Returns:
      A trained model as a dictionary with keys:
        tree: A TreeStructure instance with the learned latent structure.
        suffstats: Sufficient statistics of features, vertices, and
          edges.
        assignments: An [N, V] numpy array of latent cluster ids for each
          cell in the dataset.
    """
    return TreeCatTrainer(data, config).train()


def get_annealing_schedule(num_rows, config):
    """Iterator for subsample annealing yielding (action, arg) pairs.

    Actions are one of: 'add_row', 'remove_row', or 'sample_tree'.
    The add and remove actions each provide a row_id arg.
    """
    # Randomly shuffle rows.
    row_ids = list(range(num_rows))
    np.random.shuffle(row_ids)
    row_to_add = itertools.cycle(row_ids)
    row_to_remove = itertools.cycle(row_ids)

    # Use a linear annealing schedule.
    epochs = float(config['learning_annealing_epochs'])
    add_rate = epochs
    remove_rate = epochs - 1.0
    state = epochs * config['learning_annealing_init_rows']

    # Sample the tree after each batch.
    sampling_tree = (config['learning_sample_tree_steps'] > 0)
    num_fresh = 0
    num_stale = 0
    while num_fresh + num_stale != num_rows:
        if state >= 0.0:
            yield 'add_row', next(row_to_add)
            state -= remove_rate
            num_fresh += 1
        else:
            yield 'remove_row', next(row_to_remove)
            state += add_rate
            num_stale -= 1
        if sampling_tree and num_stale == 0 and num_fresh > 0:
            yield 'sample_tree', None
            num_stale = num_fresh
            num_fresh = 0
