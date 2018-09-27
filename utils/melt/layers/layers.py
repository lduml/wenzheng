#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ==============================================================================
#          \file   layers.py
#        \author   chenghuige  
#          \date   2016-08-19 23:22:44.032101
#   \Description  
# ==============================================================================

  
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
#since not from melt.layers.layers import * this is safe
from tensorflow.contrib.layers.python.layers import utils
import tensorflow.contrib.slim as slim

import functools
import six

from tensorflow.contrib.framework.python.ops import add_arg_scope
from tensorflow.contrib.framework.python.ops import variables
from tensorflow.contrib.layers.python.layers import initializers
from tensorflow.contrib.layers.python.layers import utils
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import sparse_tensor
from tensorflow.python.layers import convolutional as convolutional_layers
from tensorflow.python.layers import core as core_layers
from tensorflow.python.layers import  normalization as normalization_layers
from tensorflow.python.layers import pooling as pooling_layers
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn
from tensorflow.python.ops import sparse_ops
from tensorflow.python.ops import standard_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables as tf_variables
from tensorflow.python.training import moving_averages

import melt

logging = melt.logging

from melt import dropout, softmax_mask
from melt.rnn import OutputMethod, encode_outputs

# TODO -------------------------
# just use below really layers!
# TODO batch_dim to batch_axis

keras = tf.keras
layers = tf.keras.layers
Layer = layers.Layer

class MaxPooling(Layer):
  def call(self, outputs, sequence_length=None, axis=1, reduce_func=tf.reduce_max):
    return melt.max_pooling(outputs, sequence_length, axis, reduce_func)

class MaxPooling2(Layer):
  def call(self, outputs, sequence_length, sequence_length2, axis=1, reduce_func=tf.reduce_max):
    return melt.max_pooling2(outputs, sequence_length, sequence_length2, axis, reduce_func)

class MeanPooling(Layer):
  def call(self, outputs, sequence_length=None, axis=1):
    return melt.mean_pooling(outputs, sequence_length, axis)

class FirstPooling(Layer):
  def call(self, outputs, sequence_length=None, axis=1):
    return outputs[:, 0, :]

class LastPooling(Layer):
  def call(self, outputs, sequence_length=None, axis=1):
    return melt.dynamic_last_relevant(outputs, sequence_length)

class HierEncode(Layer):
  def call(self, outputs, sequence_length=None, window_size=3, axis=1):
    return melt.hier_encode(outputs, sequence_length, window_size=3, axis=1)

class TopKPooling(Layer):
  def __init__(self,  
               top_k,
               **kwargs):
    super(TopKPooling, self).__init__(**kwargs)
    self.top_k = top_k

  # def compute_output_shape(self, input_shape):
  #   return (input_shape[0], (input_shape[2] * self.top_k))
  
  def call(self, outputs, sequence_length=None, axis=1):
    x = melt.top_k_pooling(outputs, self.top_k, sequence_length, axis).values  
    #return tf.reshape(x, [melt.get_shape(outputs, 0), -1])
    return tf.reshape(x, [-1, melt.get_shape(outputs, -1) * self.top_k])


# TODO check which is better tf.nn.tanh or tf.nn.relu, by paper default should be tanh
# TODO check your topk,att cases before use relu.. seems tanh worse then relu, almost eqaul but relu a bit better and stable
class AttentionPooling(keras.Model):
  def __init__(self,  
               hidden_size=128,
               #activation=tf.nn.tanh,  
               activation=tf.nn.relu,
               **kwargs):
    super(AttentionPooling, self).__init__(**kwargs)
    self.activation = activation
    if hidden_size is not None:
      self.dense = layers.Dense(hidden_size, activation=activation)
    else:
      self.dense = None
    self.logits = layers.Dense(1, activation=None)
    self.step = -1

  def call(self, outputs, sequence_length=None, axis=1):
    self.step += 1
    if self.step == 0 and self.dense is None:
      self.dense = layers.Dense(melt.get_shape(outputs, -1), activation=self.activation)
    x = self.dense(outputs)
    logits = self.logits(x)
    alphas = tf.nn.softmax(logits) if sequence_length is None else  melt.masked_softmax(logits, sequence_length)
    encoding = tf.reduce_sum(outputs * alphas, 1)
    # [batch_size, sequence_length, 1] -> [batch_size, sequence_length]
    self.alphas = tf.squeeze(alphas, -1)    
    #self.alphas = alphas
    tf.add_to_collection('self_attention', self.alphas) 
    return encoding

class Pooling(keras.Model):
  def __init__(self,  
               name,
               top_k=2,
               #att_activation=tf.nn.tanh,
               att_activation=tf.nn.relu,
               **kwargs):
    super(Pooling, self).__init__(**kwargs)

    self.top_k = top_k

    self.poolings = []
    def get_pooling(name):
      if name == 'max':
        return MaxPooling()
      elif name == 'mean':
        return MeanPooling()
      elif name == 'attention' or name == 'att':
        return AttentionPooling(activation=att_activation)
      elif name == 'attention2' or name == 'att2':
        return AttentionPooling(hidden_size=None, activation=att_activation)
      elif name == 'topk' or name == 'top_k':
        return TopKPooling(top_k)
      elif name =='first':
        return FirstPooling()
      elif name == 'last':
        return LastPooling()
      else:
        raise f'Unsupport pooling now:{name}'

    self.names = name.split(',')
    for name in self.names:
      self.poolings.append(get_pooling(name))
  
  def call(self, outputs, sequence_length=None, axis=1, calc_word_scores=False):
    results = []
    self.word_scores = []
    for i, pooling in enumerate(self.poolings):
      results.append(pooling(outputs, sequence_length, axis))
      if calc_word_scores:
        self.word_scores.append(melt.get_words_importance(outputs, sequence_length, top_k=self.top_k, method=self.names[i]))
    
    return tf.concat(results, -1)

class DynamicDense(keras.Model):
  def __init__(self,  
               ratio,
               activation=None,
               use_bias=True,
               **kwargs):
    super(DynamicDense, self).__init__(**kwargs)
    self.ratio = ratio  
    self.activation = activation
    self.use_bais = use_bias

    self.step = -1

  def call(self, x):
    self.step += 1
    if self.step == 0:
      self.dense = layers.Dense(melt.get_shape(x, -1) * self.ratio, self.activation, self.use_bais)
    return self.dense(x)

class Embedding(keras.layers.Layer):
  def __init__(self, height, width, name='init_fw'):
    super(Embedding, self).__init__()
    initializer = 'uniform'
    self.embedding = self.add_variable(
        "embedding_kernel",
        shape=[height, width],
        dtype=tf.float32,
        initializer=initializer,
        trainable=True)

  def call(self, x=None):
    if x is not None:
      return tf.nn.embedding_lookup(self.embedding, x)
    else:
      return self.embedding


class Dropout(keras.layers.Layer):
  def __init__(self, rate, noise_shape=None, seed=None, **kwargs):
    super(Dropout).__init__(self, **kwargs)
    self.rate = rate 
    self.noise_shape = noise_shape
    self.seed = seed
  
  def call(self, x, training=False):
    if not training or self.rate <= 0.:
      return x
    else:
      scale = 1.
      shape = tf.shape(x)
      if mode == 'embedding':
        noise_shape = [shape[0], 1]
        scale = 1 - self.rate
      elif mode == 'recurrent' and len(x.get_shape().as_list()) == 3:
        noise_shape = [shape[0], 1, shape[-1]] 
      return tf.nn.dropout(x, 1 - self.rate, noise_shape=noise_shape) * scale


class Gate(keras.Model):
  def __init__(self,
               keep_prob=1.0,
               **kwargs):
    super(Gate, self).__init__(**kwargs)
    self.keep_prob = keep_prob
    self.step = -1

  def call(self, x, y, training=False):
    self.step += 1
    #with tf.variable_scope(self.scope):
    res = tf.concat([x, y], axis=2)
    dim = melt.get_shape(res, -1)
    d_res = dropout(res, keep_prob=self.keep_prob, training=training)
    if self.step == 0:
      self.dense = layers.Dense(dim, use_bias=False, activation=tf.nn.sigmoid)
    gate = self.dense(d_res)
    return res * gate

class SemanticFusion(keras.Model):
  def __init__(self,
               keep_prob=1.0,
               **kwargs):
    super(SemanticFusion, self).__init__(**kwargs)
    self.keep_prob = keep_prob
    self.step = -1

  def call(self, x, fusions, training=False):
    self.step += 1
    assert len(fusions) > 0
    vectors = tf.concat([x] + fusions, axis=-1) # size = [batch_size, ..., input_dim * (len(fusion_vectors) + 1)]
    dim = melt.get_shape(x, -1)
    dv = dropout(vectors, keep_prob=self.keep_prob, training=training)
    if self.step == 0:
      self.composition_dense = layers.Dense(dim, use_bias=True, activation=tf.nn.tanh, name='compostion_dense')
      self.gate_dense = layers.Dense(dim, use_bias=True, activation=tf.nn.sigmoid, name='gate_dense')
    r = self.composition_dense(dv)
    g = self.gate_dense(dv)
    return g * r + (1 - g) * x    

class SemanticFusionCombine(keras.Model):
  def __init__(self,
                keep_prob=1.0,
                **kwargs):
      super(SemanticFusionCombine, self).__init__(**kwargs)
      self.keep_prob = keep_prob
      self.sfu = SemanticFusion(keep_prob=keep_prob)
      self.step = -1

  def call(self, x, y, training=False):
    self.step += 1
    if melt.get_shape(x, -1) != melt.get_shape(y, -1):
      if self.step == 0:
        self.dense = layers.Dense(melt.get_shape(x, -1), activation=None, name='sfu_dense')
      y = self.dense(y)
    return self.sfu(x, [y, x * y, x - y], training=training)
  
class DotAttention(keras.Model):
  def __init__(self,
               hidden,
               keep_prob=1.0,
               combiner='gate',
               **kwargs):
    super(DotAttention, self).__init__(**kwargs)
    self.hidden = hidden
    self.keep_prob = keep_prob
    self.combiner = combiner

    self.inputs_dense = layers.Dense(hidden, use_bias=False, activation=tf.nn.relu, name='inputs_dense')
    self.memory_dense = layers.Dense(hidden, use_bias=False, activation=tf.nn.relu, name='memory_dense')

    if combiner == 'gate':
      self.combine = Gate(keep_prob=keep_prob)
    elif combiner == 'sfu':
      self.combine = SemanticFusionCombine(keep_prob=keep_prob)
    else:
      raise ValueError(combiner)

  def call(self, inputs, memory, mask, training=False):
    combiner = self.combiner
    # DotAttention already convert to dot_attention
    #with tf.variable_scope(self.scope):
    d_inputs = dropout(inputs, keep_prob=self.keep_prob, training=training)
    d_memory = dropout(memory, keep_prob=self.keep_prob, training=training)
    JX = tf.shape(inputs)[1]
    
    # TODO remove scope ?
    with tf.variable_scope("attention"):
      inputs_ = self.inputs_dense(d_inputs)
      memory_ = self.memory_dense(d_memory)

      weights = tf.matmul(inputs_, tf.transpose(memory_, [0, 2, 1])) / (self.hidden ** 0.5)
      
      if mask is not None:
        mask = tf.tile(tf.expand_dims(mask, axis=1), [1, JX, 1])
        #print(inputs_.shape, memory_.shape, weights.shape, mask.shape)
        # (32, 318, 100) (32, 26, 100) (32, 318, 26) (32, 318, 26)
        probs = tf.nn.softmax(softmax_mask(weights, mask))
      else:
        probs = tf.nn.softmax(weights)

      self.probs = probs
      # logits (32, 326, 326)  memory(32, 326, 200)
      outputs = tf.matmul(probs, memory)

    return self.combine(inputs, outputs, training=training)

# https://arxiv.org/pdf/1611.01603.pdf
class BiDAFAttention(keras.Model):
  def __init__(self,
               hidden,
               keep_prob=1.0,
               combiner='gate',
               **kwargs):
    super(BiDAFAttention, self).__init__(**kwargs)
    self.hidden = hidden
    self.keep_prob = keep_prob
    self.combiner = combiner

    self.inputs_dense = layers.Dense(hidden, use_bias=False, activation=tf.nn.relu, name='inputs_dense')
    self.memory_dense = layers.Dense(hidden, use_bias=False, activation=tf.nn.relu, name='memory_dense')

    if combiner == 'gate':
      self.combine = Gate(keep_prob=keep_prob)
    elif combiner == 'sfu':
      self.combine = SemanticFusionCombine(keep_prob=keep_prob)
    else:
      raise ValueError(combiner)

  def call(self, inputs, memory, inputs_mask, memory_mask, training=False):
    combiner = self.combiner
    # DotAttention already convert to dot_attention
    #with tf.variable_scope(self.scope):
    d_inputs = dropout(inputs, keep_prob=self.keep_prob, training=training)
    d_memory = dropout(memory, keep_prob=self.keep_prob, training=training)
    JX = tf.shape(inputs)[1]
    
    with tf.variable_scope("attention"):
      inputs_ = self.inputs_dense(d_inputs)
      memory_ = self.memory_dense(d_memory)

      # shared matrix for c2q and q2c attention
      weights = tf.matmul(inputs_, tf.transpose(memory_, [0, 2, 1])) / (self.hidden ** 0.5)

      # c2q attention
      mask = memory_mask
      if mask is not None:
        mask = tf.tile(tf.expand_dims(mask, axis=1), [1, JX, 1])
        weights = softmax_mask(weights, mask)

      probs = tf.nn.softmax(weights)
      self.probs = probs
      c2q = tf.matmul(probs, memory)

      # q2c attention
      # (batch_size, clen)
      logits = tf.reduce_max(weights, -1) 
      mask = inputs_mask
      if mask is not None:
        logits = softmax_mask(logits, mask)
      probs = tf.nn.softmax(logits)
      # inputs (batch_size, clen, dim), probs (batch_size, clen)
      q2c = tf.matmul(tf.expand_dims(probs, 1), inputs)
      # (batch_size, clen, dim)
      q2c = tf.tile(q2c, [1, JX, 1])

      outputs = tf.concat([c2q, q2c], -1)

    return self.combine(inputs, outputs, training=training)

      
