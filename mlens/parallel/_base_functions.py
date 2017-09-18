"""
Functions for base computations
"""

import os
import numpy as np
from time import sleep
from scipy.sparse import issparse
from copy import deepcopy

from ..externals.joblib import delayed
from ..utils import pickle_load, pickle_save
from ..utils.exceptions import (ParallelProcessingError,
                                ParallelProcessingWarning)

try:
    from time import perf_counter as time_
except ImportError:
    from time import time as time_

import warnings


# Default params
IVALS = (0.01, 120)


###############################################################################
# Base estimation jobs

def fit(inst, X, y, P, dir, parallel):
    """Fit layer through given attribute."""
    # Set estimator and transformer lists to loop over, and collect
    # estimator column ids for the prediction matrix
    inst._format_instance_list()
    inst._get_col_id()

    # Auxiliary variables
    preprocess = inst.t is not None
    pred_method = inst.layer._predict_attr

    if preprocess:
        parallel(delayed(fit_trans)(dir=dir,
                                    case=case,
                                    inst=instance_list,
                                    x=X,
                                    y=y,
                                    idx=tri)
                 for case, tri, _, instance_list in inst.t)

    parallel(delayed(fit_est)(dir=dir,
                              case=case,
                              inst_name=inst_name,
                              inst=instance,
                              x=X,
                              y=y,
                              pred=P if tei is not None else None,
                              idx=(tri, tei, inst.c[case, inst_name]),
                              name=inst.layer.name,
                              raise_on_exception=inst.layer.raise_on_exception,
                              preprocess=preprocess,
                              ivals=IVALS,
                              attr=pred_method,
                              scorer=inst.layer.scorer)
             for case, tri, tei, instance_list in inst.e
             for inst_name, instance in instance_list)
    assemble(inst)


def predict(inst, X, P, parallel):
    """Predict full X array using fitted layer."""
    inst._check_fitted()
    prep, ests = inst._retrieve('full')

    parallel(delayed(predict_est)(tr_list=deepcopy(prep[case])
                                  if prep is not None else [],
                                  est=est,
                                  x=X,
                                  pred=P,
                                  col=col,
                                  attr=inst.layer._predict_attr)
             for case, (_, est, (_, col)) in ests)


def transform(inst, X, P, parallel):
    """Transform training data with fold-estimators, as in ``fit`` call."""
    inst._check_fitted()
    prep, ests = inst._retrieve('fold')

    parallel(delayed(predict_fold_est)(tr_list=deepcopy(prep[case])
                                       if prep is not None else [],
                                       est=est,
                                       x=X,
                                       pred=P,
                                       idx=idx,
                                       attr=inst.layer._predict_attr)
             for case, (est_name, est, idx) in ests)

###############################################################################
# Estimation functions


def predict_est(tr_list, est, x, pred, col, attr):
    """Method for predicting with fitted transformers and estimators."""
    x, _ = _slice_array(x, None, None)

    for tr_name, tr in tr_list:
        x = tr.transform(x)

    p = getattr(est, attr)(x)

    if len(p.shape) == 1:
        pred[:, col] = p
    else:
        pred[:, col:(col + p.shape[1])] = p


def predict_fold_est(tr_list, est, x, pred, idx, attr):
    """Method for predicting with transformers and estimators from fit call."""
    tei, col = idx[0], idx[1]
    n = x.shape[0]

    x, _ = _slice_array(x, None, tei)

    for tr_name, tr in tr_list:
        x = tr.transform(x)

    p = getattr(est, attr)(x)
    _assign_predictions(pred, p, tei, col, n)


def fit_trans(dir, case, inst, x, y, idx):
    """Fit transformers and write to cache."""
    x, y = _slice_array(x, y, idx)

    out = []
    for tr_name, tr in inst:
        # Fit transformer
        tr.fit(x, y)

        # If more than one step, transform input for next step.
        if len(inst) > 1:
            x, y = _transform(tr, x, y)

        out.append((tr_name, tr))

    # Write transformer list to cache
    f = os.path.join(dir, '%s__t' % case)
    pickle_save(out, f)


def fit_est(dir, case, inst_name, inst, x, y, pred, idx, raise_on_exception,
            preprocess, name, ivals, attr, scorer=None):
    """Fit estimator and write to cache along with predictions."""
    # Have to be careful in prepping data for estimation.
    # We need to slice memmap and convert to a proper array - otherwise
    # estimators can store results memmaped to the cache, which will
    # prevent the garbage collector from releasing memmaps from memory
    n = x.shape[0]
    xtemp, ytemp = _slice_array(x, y, idx[0])

    # Load transformers
    if preprocess:
        f = os.path.join(dir, '%s__t' % case)
        tr_list = _load_trans(f, case, ivals, raise_on_exception)
    else:
        tr_list = []

    # Transform input (triggers copying)
    for tr_name, tr in tr_list:
        xtemp, ytemp = _transform(tr, xtemp, ytemp)

    # Fit estimator
    inst.fit(xtemp, ytemp)

    # Predict if asked
    if idx[1]:
        tei = idx[1]
        col = idx[2]

        xtemp, ytemp = _slice_array(x, y, tei)

        for tr_name, tr in tr_list:
            xtemp = tr.transform(xtemp)

        p = getattr(inst, attr)(xtemp)

        # Assign predictions to matrix
        _assign_predictions(pred, p, tei, col, n)

        # Score predictions if applicable
        s = _score_predictions(ytemp, p, scorer, name, inst_name)

        idx = idx[1:]  # format as (tei, col)
    else:
        idx = (None, idx[2])  # format as (None, col), where None => all obs
        s = None

    f = os.path.join(dir, '%s__%s__e' % (case, inst_name))
    pickle_save((inst_name, inst, idx, s), f)


###############################################################################
# Helpers
def assemble(inst):
    """Store fitted transformer and estimators in the layer."""
    inst.layer.preprocessing_ = _assemble(inst.dir, inst.t, 't')
    inst.layer.estimators_, s = _assemble(inst.dir, inst.e, 'e')

    if inst.layer.scorer is not None and inst.layer.cls is not 'full':
        inst.layer.scores_ = inst._build_scores(s)


def construct_args(func, job):
    """Helper to construct argument list from a ``job`` instance."""
    fargs = func.__code__.co_varnames

    # Strip undesired variables
    args = [a for a in fargs if a not in {'parallel', 'X', 'P', 'self'}]

    kwargs = {a: getattr(job, a) for a in args if a in job.__slots__}

    if 'X' in fargs:
        kwargs['X'] = job.predict_in
    if 'P' in fargs:
        kwargs['P'] = job.predict_out
    return kwargs


def _slice_array(x, y, idx, r=0):
    """Build training array index and slice data."""
    if idx == 'all':
        idx = None

    if idx:
        # Check if the idx is a tuple and if so, whether it can be made
        # into a simple slice
        if isinstance(idx[0], tuple):
            if len(idx[0]) > 1:
                # Advanced indexing is required. This will trigger a copy
                # of the slice in question to be made
                simple_slice = False
                idx = np.hstack([np.arange(t0 - r, t1 - r) for t0, t1 in idx])
                x = x[idx]
                y = y[idx] if y is not None else y
            else:
                # The tuple is of the form ((a, b),) and can be made
                # into a simple (a, b) tuple for which basic slicing applies
                # which allows a view to be returned instead of a copy
                simple_slice = True
                idx = idx[0]
        else:
            # Index tuples of the form (a, b) allows simple slicing
            simple_slice = True

        if simple_slice:
            x = x[slice(idx[0] - r, idx[1] - r)]
            y = y[slice(idx[0] - r, idx[1] - r)] if y is not None else y

    # Cast as ndarray to avoid passing memmaps to estimators
    if y is not None:
        y = y.view(type=np.ndarray)
    if not issparse(x):
        x = x.view(type=np.ndarray)

    return x, y


def _assign_predictions(pred, p, tei, col, n):
    """Assign predictions to memmaped prediction array."""
    if tei == 'all':
        # Assign to all data
        pred[:, col] = p
    else:
        r = n - pred.shape[0]

        if isinstance(tei[0], tuple):
            if len(tei) > 1:
                idx = np.hstack([np.arange(t0 - r, t1 - r) for t0, t1 in tei])
            else:
                tei = tei[0]
                idx = slice(tei[0] - r, tei[1] - r)
        else:
            idx = slice(tei[0] - r, tei[1] - r)

        if len(p.shape) == 1:
            pred[idx, col] = p
        else:
            pred[(idx, slice(col, col + p.shape[1]))] = p


def _score_predictions(y, p, scorer, name, inst_name):
    s = None
    if scorer is not None:
        try:
            s = scorer(y, p)
        except Exception as exc:
            warnings.warn("[%s] Could not score %s. Details:\n%r" %
                          (name, inst_name, exc),
                          ParallelProcessingWarning)
    return s


def _assemble(dir, instance_list, suffix):
    """Utility for loading fitted instances."""
    if suffix is 't':
        if instance_list is None:
            return

        return [(tup[0],
                 pickle_load(os.path.join(dir, '%s__%s' % (tup[0], suffix))))
                for tup in instance_list]
    else:
        # We iterate over estimators to split out the estimator info and the
        # scoring info (if any)
        ests_ = []
        scores_ = []
        for tup in instance_list:
            for etup in tup[-1]:
                f = os.path.join(dir, '%s__%s__%s' % (tup[0], etup[0], suffix))
                loaded = pickle_load(f)

                # split out the scores, the final element in the l tuple
                ests_.append((tup[0], loaded[:-1]))

                case = '%s___' % tup[0] if tup[0] is not None else '___'
                scores_.append((case + etup[0], loaded[-1]))

        return ests_, scores_


def _transform(tr, x, y):
    """Try transforming with X and y. Else, transform with only X."""
    try:
        x = tr.transform(x)
    except TypeError:
        x, y = tr.transform(x, y)

    return x, y


def _load_trans(dir, case, ivals, raise_on_exception):
    """Try loading transformers, and handle exception if not ready yet."""
    s = ivals[0]
    lim = ivals[1]
    try:
        # Assume file exists
        return pickle_load(dir)
    except (OSError, IOError) as exc:
        # We would expect an OSError, but Python 2.7 we get an IOError
        msg = str(exc)
        error_msg = ("The file %s cannot be found after %i seconds of "
                     "waiting. Check that time to fit transformers is "
                     "sufficiently fast to complete fitting before "
                     "fitting estimators. Consider reducing the "
                     "preprocessing intensity in the ensemble, or "
                     "increase the '__lim__' attribute to wait extend "
                     "period of waiting on transformation to complete."
                     " Details:\n%r")

        # Wait and check if transformer is readied.
        ts = time_()
        while not os.path.exists(dir):

            sleep(s)

            if time_() - ts > lim:
                # If timeout limit is reached, raise error
                if raise_on_exception:
                    raise ParallelProcessingError(error_msg % (dir, lim, msg))

                warnings.warn("Transformer %s not found in cache (%s). "
                              "Will check every %.1f seconds for %i seconds "
                              "before aborting. " % (case, dir, s, lim),
                              ParallelProcessingWarning)

                # If not raise_on_exception, we set it to True now to ensure
                # a second timeout aborts the job
                raise_on_exception = True
                ts = time_()

        return pickle_load(dir)
