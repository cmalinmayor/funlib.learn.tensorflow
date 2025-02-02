from .impl import emst as euclidean_mst
from .impl import um_loss
from .py_func_gradient import py_func_gradient
import logging
import numpy as np
import tensorflow as tf

logger = logging.getLogger(__name__)


def get_emst(embedding):

    emst = euclidean_mst(embedding.astype(np.float64))

    d_min = np.min(emst[:, 2])
    d_max = np.max(emst[:, 2])
    logger.info("min/max ultrametric: %f/%f", d_min, d_max)

    return emst


def get_emst_op(embedding, name=None):

    return tf.py_func(
        get_emst,
        [embedding],
        [tf.float64],
        name=name,
        stateful=False)[0]


def get_um_loss(mst, dist, gt_seg, alpha):
    '''Compute the ultra-metric loss given an MST and segmentation.

    Args:

        mst (Tensor, shape ``(3, n-1)``): u, v indices and distance of edges of
            the MST spanning n nodes.

        dist (Tensor, shape ``(n-1)``): The distances of the edges. This
            argument will be ignored, it is used only to communicate to
            tensorflow that there is a dependency on distances. The distances
            actually used are the ones in parameter ``mst``.

        gt_seg (Tensor, arbitrary shape): The label of each node. Will be
            flattened. The indices in mst should be valid indices into this
            array.

        alpha (Tensor, single float): The margin value of the quadrupel loss.

    Returns:

        A tuple::

            (loss, ratio_pos, ratio_neg)

        Except for ``loss``, each entry is a tensor of shape ``(n-1,)``,
        corresponding to the edges in the MST. ``ratio_pos`` and ``ratio_neg``
        are the ratio of positive and negative pairs that share an edge, of the
        total number of positive and negative pairs.
    '''

    # We don't use 'dist' here, it is already contained in the mst. It is
    # passed here just so that tensorflow knows there is dependecy to the
    # ouput.
    (loss, _, ratio_pos, ratio_neg, num_pairs_pos, num_pairs_neg) = um_loss(
        mst,
        gt_seg,
        alpha)

    return (
        np.float32(loss),
        ratio_pos.astype(np.float32),
        ratio_neg.astype(np.float32),
        np.float32(num_pairs_pos),
        np.float32(num_pairs_neg))


def get_um_loss_gradient(mst, dist, gt_seg, alpha):
    '''Compute the ultra-metric loss gradient given an MST and segmentation.

    Args:

        mst (Tensor, shape ``(3, n-1)``): u, v indices and distance of edges of
            the MST spanning n nodes.

        dist (Tensor, shape ``(n-1)``): The distances of the edges. This
            argument will be ignored, it is used only to communicate to
            tensorflow that there is a dependency on distances. The distances
            actually used are the ones in parameter ``mst``.

        gt_seg (Tensor, arbitrary shape): The label of each node. Will be
            flattened. The indices in mst should be valid indices into this
            array.

        alpha (Tensor, single float): The margin value of the quadrupel loss.

    Returns:

        A Tensor containing the gradient on the distances.
    '''

    # We don't use 'dist' here, it is already contained in the mst. It is
    # passed here just so that tensorflow knows there is dependecy to the
    # ouput.
    (_, gradient, _, _, _, _) = um_loss(
        mst,
        gt_seg,
        alpha)

    return gradient.astype(np.float32)


def get_um_loss_gradient_op(
        op,
        dloss,
        dratio_pos,
        dratio_neg,
        dnum_pairs_pos,
        dnum_pairs_neg):

    gradient = tf.py_func(
        get_um_loss_gradient,
        [x for x in op.inputs],
        [tf.float32],
        stateful=False)[0]

    return (None, gradient*dloss, None, None)


def ultrametric_loss_op(
        embedding,
        gt_seg,
        mask=None,
        alpha=0.1,
        add_coordinates=True,
        coordinate_scale=1.0,
        pretrain=False,
        pretrain_balance=False,
        name=None):
    '''Returns a tensorflow op to compute the ultra-metric quadrupel loss::

        L = sum_p sum_n max(0, d(n) - d(p) + alpha)^2

    where ``p`` and ``n`` are pairs points with same and different labels,
    respectively, and ``d(.)`` the ultrametric distance between the points.

    Args:

        embedding (Tensor, shape ``(k, d, h, w)``):

            A k-dimensional feature embedding of points in 3D.

        gt_seg (Tensor, shape ``(d, h, w)``):

            The ground-truth labels of the points.

        mask (optional, Tensor, shape ``(d, h, w)``):

            If given, consider only points that are not zero in the mask.

        alpha (optional, float):

            The margin term of the quadrupel loss.

        add_coordinates (optional, bool):

            If ``True``, add the ``(z, y, x)`` coordinates of the points to the
            embedding.

        coordinate_scale(optional, ``float`` or ``tuple`` of ``float``):

            How to scale the coordinates, if used to augment the embedding.

        pretrain (optional, ``bool``):

            Instead of computing the loss on all quadrupels, compute it on
            pairs only. The loss of positive pairs is the Euclidean distance of
            their maximin edge squared, of negative pairs ``max(0, alpha -
            distance)`` squared.

        pretrain_balance (optional, ``bool``):

            If ``false`` (the default), the total loss is the sum of positive
            pair losses and negative pair losses, divided by the total number
            of pairs. This puts more emphasis on the set of pairs (positive or
            negative) that occur more frequently.

            If ``true``, the total loss is the sum of positive pair losses and
            negative pair losses; each divided by the number of positive and
            negative pairs, respectively. This puts equal emphasis on positive
            and negative pairs, independent of the number of positive and
            negative pairs.

        name (optional, ``string``):

            An optional name for the operator.

    Returns:

        A tuple ``(loss, emst, edges_u, edges_v, dist)``, where ``loss`` is a
        scalar, ``emst`` a tensor holding the MST edges as pairs of nodes,
        ``edges_u`` and ``edges_v`` the respective embeddings of each edges,
        and ``dist`` the length of the edges.
    '''

    # We get the embedding as a tensor of shape (k, d, h, w).
    k, depth, height, width = embedding.shape.as_list()

    # 1. Augmented by spatial coordinates, if requested.

    if add_coordinates:

        try:
            scale = tuple(coordinate_scale)
        except TypeError:
            scale = (coordinate_scale,)*3

        coordinates = tf.meshgrid(
            np.arange(0, depth*scale[0], scale[0]),
            np.arange(0, height*scale[1], scale[1]),
            np.arange(0, width*scale[2], scale[2]),
            indexing='ij')
        for i in range(len(coordinates)):
            coordinates[i] = tf.cast(coordinates[i], tf.float32)
        embedding = tf.concat([embedding, coordinates], 0)

        max_scale = max(scale)
        min_scale = min(scale)
        min_d = min_scale
        max_d = np.sqrt(max_scale**2 + k)

        if (max_d - min_d) < alpha:
            logger.warn(
                "Your alpha is too big: min and max ultrametric between any "
                "pair of points is %f and %f (this assumes your embedding is "
                "in [0, 1], if it is not, you might ignore this warning)",
                min_d, max_d)

    # 2. Transpose into tensor (d*h*w, k+3), i.e., one embedding vector per
    #    node, augmented by spatial coordinates if requested.

    embedding = tf.transpose(embedding, perm=[1, 2, 3, 0])
    embedding = tf.reshape(embedding, [depth*width*height, -1])
    gt_seg = tf.reshape(gt_seg, [depth*width*height])

    if mask is not None:
        mask = tf.reshape(mask, [depth*width*height])
        embedding = tf.boolean_mask(embedding, mask)
        gt_seg = tf.boolean_mask(gt_seg, mask)

    # 3. Get the EMST on the embedding vectors.

    emst = get_emst_op(embedding)

    # 4. Compute the lengths of EMST edges

    edges_u = tf.gather(embedding, tf.cast(emst[:, 0], tf.int64))
    edges_v = tf.gather(embedding, tf.cast(emst[:, 1], tf.int64))
    dist_squared = tf.reduce_sum(tf.square(tf.subtract(edges_u, edges_v)), 1)
    dist = tf.sqrt(dist_squared)

    # 5. Compute the UM loss

    alpha = tf.constant(alpha, dtype=tf.float32)

    if pretrain:

        # we need the um_loss just to get the ratio_pos, ratio_neg, and the
        # total number of positive and negative pairs
        _, ratio_pos, ratio_neg, num_pairs_pos, num_pairs_neg = tf.py_func(
            get_um_loss,
            [emst, dist, gt_seg, alpha],
            [tf.float32, tf.float32, tf.float32, tf.float32, tf.float32],
            name=name,
            stateful=False)

        loss_pos = tf.multiply(
            dist_squared,
            ratio_pos)
        loss_neg = tf.multiply(
            tf.square(tf.maximum(0.0, alpha - dist)),
            ratio_neg)

        if pretrain_balance:

            # the ratios returned by get_um_loss are already class balanced,
            # there is nothing more to do than to add the losses up
            loss = tf.reduce_sum(loss_pos) + tf.reduce_sum(loss_neg)

        else:

            # denormalize the ratios, add them up, and divide by the total
            # number of pairs
            sum_pos = tf.reduce_sum(loss_pos)*num_pairs_pos
            sum_neg = tf.reduce_sum(loss_neg)*num_pairs_neg
            num_pairs = num_pairs_pos + num_pairs_neg

            loss = (sum_pos + sum_neg)/num_pairs

    else:

        loss = py_func_gradient(
            get_um_loss,
            [emst, dist, gt_seg, alpha],
            [tf.float32, tf.float32, tf.float32, tf.float32, tf.float32],
            gradient_op=get_um_loss_gradient_op,
            name=name,
            stateful=False)[0]

    return (loss, emst, edges_u, edges_v, dist)
