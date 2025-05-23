#  Copyright 2024 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
""" Mixture of Experts (MoE) tests. """

import os.path
import unittest
from typing import Tuple

import pytest

import jax
import jax.numpy as jnp
from jax.sharding import Mesh

import flax.linen as nn
from flax.linen import partitioning as nn_partitioning

from MaxText import maxtext_utils
from MaxText import pyconfig
from MaxText.common_types import Config, DType
from MaxText.globals import PKG_DIR
from MaxText.layers import linears
from MaxText.layers import moe
from MaxText.layers.initializers import NdInitializer, nd_dense_init


class TokenDroppingTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="token_dropping_test",
        enable_checkpointing=False,
        model_name="mixtral-8x7b",
        dtype="bfloat16",
        megablox=False,
        sparse_matmul=False,
        max_target_length=80,
        per_device_batch_size=1,
        capacity_factor=2,
    )
    self.rng = jax.random.PRNGKey(42)
    devices_array = maxtext_utils.create_device_mesh(self.cfg)
    self.model = moe.RoutedMoE(
        name="MoeBlock",
        config=self.cfg,
        num_experts=self.cfg.num_experts,
        num_experts_per_tok=self.cfg.num_experts_per_tok,
        mesh=Mesh(devices_array, self.cfg.mesh_axes),
        kernel_init=nd_dense_init(1.0, "fan_in", "truncated_normal"),
        kernel_axes=("embed", "mlp"),
        dtype=self.cfg.dtype,
    )

  def test_generate_masks(self):
    # expert_capacity = (tokens_per_batch / num_experts) * capacity_factor
    # expert_capacity_in_batch = (4 * 2 / 8) * 2 = 2
    top_k_indices = jnp.array(
        [
            [[0, 5], [0, 4], [1, 0], [3, 5]],
            [[1, 2], [4, 1], [5, 0], [7, 1]],
            [[6, 2], [2, 3], [4, 2], [1, 2]],
            [[4, 1], [0, 7], [5, 0], [4, 7]],
        ]
    )
    softmax_probs = jnp.array(
        [
            [
                [0.20, 0, 0, 0, 0, 0.80, 0, 0],
                [0.68, 0, 0, 0, 0.32, 0, 0, 0],
                [0.22, 0.78, 0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0.32, 0, 0.68, 0, 0],
            ],
            [
                [0, 0.26, 0.74, 0, 0, 0, 0, 0],
                [0, 0.79, 0, 0, 0.21, 0, 0, 0],
                [0.89, 0, 0, 0, 0, 0.11, 0, 0],
                [0, 0.11, 0, 0, 0, 0, 0, 0.89],
            ],
            [
                [0, 0, 0.26, 0, 0, 0, 0.74, 0],
                [0, 0, 0.88, 0.12, 0, 0, 0, 0],
                [0, 0, 0.17, 0, 0.83, 0, 0, 0],
                [0, 0.35, 0.65, 0, 0, 0, 0, 0],
            ],
            [
                [0, 0.47, 0, 0, 0.53, 0, 0, 0],
                [0.36, 0, 0, 0, 0, 0, 0, 0.64],
                [0.15, 0, 0, 0, 0, 0.85, 0, 0],
                [0, 0, 0, 0, 0.18, 0, 0, 0.82],
            ],
        ]
    )

    # As expert_capacity_in_batch=2, so updated softmax_probs become (4 tokens were dropped):
    # softmax_probs = jnp.array([[[0.20, 0, 0, 0, 0, 0.80, 0, 0],
    #                             [0.68, 0, 0, 0, 0.32, 0, 0, 0],
    #                             [0, 0.78, 0, 0, 0, 0, 0, 0],
    #                             [0, 0, 0, 0.32, 0, 0.68, 0, 0]],
    #                            [[0, 0.26, 0.74, 0, 0, 0, 0, 0],
    #                             [0, 0.79, 0, 0, 0.21, 0, 0, 0],
    #                             [0.89, 0, 0, 0, 0, 0.11, 0, 0],
    #                             [0, 0, 0, 0, 0, 0, 0, 0.89]],
    #                            [[0, 0, 0.26, 0, 0, 0, 0.74, 0],
    #                             [0, 0, 0.88, 0.12, 0, 0, 0, 0],
    #                             [0, 0, 0, 0, 0.83, 0, 0, 0],
    #                             [0, 0.35, 0, 0, 0, 0, 0, 0]],
    #                            [[0, 0.47, 0, 0, 0.53, 0, 0, 0],
    #                             [0.36, 0, 0, 0, 0, 0, 0, 0.64],
    #                             [0.15, 0, 0, 0, 0, 0.85, 0, 0],
    #                             [0, 0, 0, 0, 0.18, 0, 0, 0.82]]])

    # shape of dispatch_mask & combine_mask: (batch_size, seq_len, num_experts, expert_capacity_per_batch)
    expected_combine_mask = jnp.array(
        [
            [
                [[0.2, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0.8, 0], [0, 0], [0, 0]],
                [[0, 0.68], [0, 0], [0, 0], [0, 0], [0.32, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0.78, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0.32, 0], [0, 0], [0, 0.68], [0, 0], [0, 0]],
            ],
            [
                [[0, 0], [0.26, 0], [0.74, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0.79], [0, 0], [0, 0], [0.21, 0], [0, 0], [0, 0], [0, 0]],
                [[0.89, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0.11, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0.89, 0]],
            ],
            [
                [[0, 0], [0, 0], [0.26, 0], [0, 0], [0, 0], [0, 0], [0.74, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0.88], [0.12, 0], [0, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0], [0.83, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0.35, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]],
            ],
            [
                [[0, 0], [0.47, 0], [0, 0], [0, 0], [0.53, 0], [0, 0], [0, 0], [0, 0]],
                [[0.36, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0.64, 0]],
                [[0, 0.15], [0, 0], [0, 0], [0, 0], [0, 0], [0.85, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0], [0, 0.18], [0, 0], [0, 0], [0, 0.82]],
            ],
        ],
        dtype=jnp.float32,
    )
    expected_dispatch_mask = expected_combine_mask.astype(bool)
    actual_dispatch_mask, actual_combine_mask = self.model.generate_masks(top_k_indices, softmax_probs)

    self.assertTrue((expected_dispatch_mask == actual_dispatch_mask).all())
    self.assertTrue(jax.numpy.allclose(expected_combine_mask, actual_combine_mask, rtol=1e-02, atol=1e-02))


class DeepSeekRoutingTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="deepseek_routing_test",
        enable_checkpointing=False,
        decoder_block="deepseek",
        dtype="bfloat16",
        max_target_length=2,
        max_prefill_predict_length=1,
        per_device_batch_size=1,
        n_routing_groups=4,
        topk_routing_group=2,
        num_experts=16,
        num_experts_per_tok=4,
        sparse_matmul=True,
    )
    self.rng = jax.random.PRNGKey(42)
    devices_array = maxtext_utils.create_device_mesh(self.cfg)
    self.model = moe.RoutedMoE(
        name="MoeBlock",
        config=self.cfg,
        num_experts=self.cfg.num_experts,
        num_experts_per_tok=self.cfg.num_experts_per_tok,
        mesh=Mesh(devices_array, self.cfg.mesh_axes),
        kernel_init=nd_dense_init(1.0, "fan_in", "truncated_normal"),
        kernel_axes=("embed", "mlp"),
        dtype=self.cfg.dtype,
    )

  def test_deepseek_routing(self):
    # shape as [batch, sequence, num_experts] = [1,2,16]
    gate_logits = jnp.array(
        [
            [
                [0.20, 0.10, 0.05, 0.10, 0.10, 0.60, 0.30, 0.10, 0.80, 0.01, 0.01, 0.01, 0.05, 0.80, 0.20, 0.10],
                [0.68, 0.20, 0.06, 0.03, 0.32, 0.10, 0.05, 0.02, 0.65, 0.20, 0.04, 0.01, 0.32, 0.10, 0.05, 0.02],
            ]
        ]
    )
    pre_bias_logits = gate_logits - 0.5

    # 4 groups of 1st token:
    #  [0.20, 0.10, 0.05, 0.10] - sum top2 = 0.7
    #  [0.10, 0.60, 0.30, 0.10] - sum top2 = 0.9 (selected group) - index from 4 to 7
    #  [0.80, 0.01, 0.01, 0.01] - sum top2 = 0.81
    #  [0.05, 0.80, 0.20, 0.10] - sum top2 = 1.0 (selected group) - index from 12 to 15
    #
    # 4 groups of 2st token
    #  [0.68, 0.20, 0.06, 0.03] - sum top2 = 0.88 (selected group) - index from 0 to 3
    #  [0.32, 0.10, 0.05, 0.02] - sum top2 = 0.42
    #  [0.65, 0.20, 0.04, 0.01] - sum top2 = 0.85 (selected group) - index from 8 to 11
    #  [0.32, 0.10, 0.05, 0.02] - sum top2 = 0.42
    #
    # From selected groups to choice top4 for each token
    expected_top_k_indices = jnp.array([[[13, 5, 6, 14], [0, 8, 1, 9]]])
    expected_top_k_weights = jnp.take_along_axis(pre_bias_logits, expected_top_k_indices, axis=-1)
    actual_top_k_weights, actual_top_k_indices = self.model.deepseek_routing(gate_logits, pre_bias_logits)
    self.assertTrue(
        jax.numpy.allclose(expected_top_k_indices, actual_top_k_indices, rtol=1e-05, atol=1e-05, equal_nan=False)
    )
    self.assertTrue(
        jax.numpy.allclose(expected_top_k_weights, actual_top_k_weights, rtol=1e-05, atol=1e-05, equal_nan=False)
    )


class MoeLoopBlock(nn.Module):
  """Reference implementation from https://github.com/mistralai/mistral-inference.
  This is not included anymore in our repo, due to a limitation of for-loop implementation in sharding.
  """

  config: Config
  num_experts: int
  num_experts_per_tok: int
  kernel_init: NdInitializer
  kernel_axes: Tuple[str, ...]
  weight_dtype: DType = jnp.float32
  dtype: DType = jnp.bfloat16

  @nn.compact
  def __call__(self, inputs, deterministic: bool = False):
    gate_logits = moe.GateLogit(
        self.num_experts,
        self.config.model_name,
        dtype=self.dtype,
        kernel_init=self.kernel_init,
        kernel_axes=self.kernel_axes,
        name="gate",
    )(inputs)[0]

    weights, selected_experts = jax.lax.top_k(gate_logits, self.num_experts_per_tok)
    weights = jax.nn.softmax(weights.astype(jnp.float32), axis=-1).astype(self.weight_dtype)
    mlp_lnx = jnp.zeros_like(inputs)
    mlp_lnx = nn.with_logical_constraint(mlp_lnx, ("activation_batch", "activation_length", "activation_embed"))

    for k in range(self.num_experts):
      weights_exp = jnp.sum(jnp.multiply(selected_experts == k, weights), axis=-1)
      mlp_lnx_exp = linears.MlpBlock(
          intermediate_dim=self.config.mlp_dim,
          activations=["silu", "linear"],
          intermediate_dropout_rate=self.config.dropout_rate,
          dtype=self.dtype,
          weight_dtype=self.weight_dtype,
          name=f"mlp_{k}",
          config=self.config,
      )(inputs, deterministic=deterministic)

      mlp_lnx_exp = nn.with_logical_constraint(mlp_lnx_exp, ("activation_batch", "activation_length", "activation_embed"))
      mlp_lnx_exp = weights_exp[:, :, None] * mlp_lnx_exp
      mlp_lnx += mlp_lnx_exp

    return mlp_lnx


class RoutedMoeTest(unittest.TestCase):
  """Routed Mixture of Experts test."""

  def get_expected_output(self, rng, hidden_states, cfg):
    """Retrieve expected output from Routed Mixture of Experts."""
    model = MoeLoopBlock(
        config=cfg,
        num_experts=cfg.num_experts,
        num_experts_per_tok=cfg.num_experts_per_tok,
        kernel_init=nd_dense_init(1.0, "fan_in", "truncated_normal"),
        kernel_axes=("embed", "mlp"),
        dtype=cfg.dtype,
    )
    variables = model.init(
        rng, jax.random.normal(rng, (int(cfg.per_device_batch_size), cfg.max_target_length, cfg.base_emb_dim))
    )

    output = jax.jit(model.apply)(variables, hidden_states)  # pylint: disable=not-callable
    return variables, output

  def get_moe_output(self, variables, hidden_states, cfg, mesh):
    """retrieve expected output from MoE"""
    model = moe.RoutedMoE(
        name="MoeBlock",
        config=cfg,
        num_experts=cfg.num_experts,
        num_experts_per_tok=cfg.num_experts_per_tok,
        mesh=mesh,
        kernel_init=nd_dense_init(1.0, "fan_in", "truncated_normal"),
        kernel_axes=("embed", "mlp"),
        intermediate_dim=cfg.mlp_dim,
        dtype=cfg.dtype,
    )

    # convert format of parameters
    kernel = variables["params"]["gate"]["kernel"].value
    kernel = kernel.astype(cfg.weight_dtype)

    exp_wi_0 = []
    exp_wi_1 = []
    exp_wo = []

    for i in range(cfg.num_experts):
      tmp_wi_0 = variables["params"][f"mlp_{i}"]["wi_0"]["kernel"].value
      tmp_wi_0 = jnp.reshape(tmp_wi_0, (1, cfg.base_emb_dim, cfg.base_mlp_dim))
      tmp_wi_1 = variables["params"][f"mlp_{i}"]["wi_1"]["kernel"].value
      tmp_wi_1 = jnp.reshape(tmp_wi_1, (1, cfg.base_emb_dim, cfg.base_mlp_dim))
      tmp_wo = variables["params"][f"mlp_{i}"]["wo"]["kernel"].value
      tmp_wo = jnp.reshape(tmp_wo, (1, cfg.base_mlp_dim, cfg.base_emb_dim))

      exp_wi_0.append(tmp_wi_0)
      exp_wi_1.append(tmp_wi_1)
      exp_wo.append(tmp_wo)

    wi_0 = jnp.concatenate(exp_wi_0, axis=0, dtype=cfg.weight_dtype)
    wi_1 = jnp.concatenate(exp_wi_1, axis=0, dtype=cfg.weight_dtype)
    wo = jnp.concatenate(exp_wo, axis=0, dtype=cfg.weight_dtype)

    moe_variables = {"params": {"gate": {"kernel": kernel}, "wi_0": wi_0, "wi_1": wi_1, "wo": wo}}

    output = jax.jit(model.apply)(moe_variables, hidden_states)  # pylint: disable=not-callable
    return output

  @pytest.mark.tpu_only
  def test_megablox(self):
    cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="moe_block_megablox_test",
        enable_checkpointing=False,
        model_name="mixtral-8x7b",
        dtype="bfloat16",
        megablox=True,
        sparse_matmul=True,
        per_device_batch_size=4,
    )

    rng = jax.random.PRNGKey(1234)
    rng_model, rng_hidden_states = jax.random.split(rng)
    hidden_states = jax.random.uniform(
        rng_hidden_states, (int(cfg.per_device_batch_size), cfg.max_target_length, cfg.base_emb_dim), dtype=cfg.dtype
    )

    devices_array = maxtext_utils.create_device_mesh(cfg)
    mesh = Mesh(devices_array, cfg.mesh_axes)
    variables, expected_output = self.get_expected_output(rng_model, hidden_states, cfg)
    actual_output, _ = self.get_moe_output(variables, hidden_states, cfg, mesh)
    self.assertTrue(jax.numpy.allclose(expected_output, actual_output, rtol=1e-02, atol=1e-02, equal_nan=False))

  @pytest.mark.tpu_only
  def test_ragged_dot(self):
    cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="moe_block_ragged_dot_test",
        enable_checkpointing=False,
        model_name="mixtral-8x7b",
        dtype="bfloat16",
        megablox=False,
        sparse_matmul=True,
        per_device_batch_size=4,
    )

    rng = jax.random.PRNGKey(1234)
    rng_model, rng_hidden_states = jax.random.split(rng)
    hidden_states = jax.random.uniform(
        rng_hidden_states, (int(cfg.per_device_batch_size), cfg.max_target_length, cfg.base_emb_dim), dtype=cfg.dtype
    )

    devices_array = maxtext_utils.create_device_mesh(cfg)
    mesh = Mesh(devices_array, cfg.mesh_axes)
    variables, expected_output = self.get_expected_output(rng_model, hidden_states, cfg)
    actual_output, _ = self.get_moe_output(variables, hidden_states, cfg, mesh)
    self.assertTrue(jax.numpy.allclose(expected_output, actual_output, rtol=1e-02, atol=1e-02, equal_nan=False))

  @pytest.mark.tpu_only
  def test_dense(self):
    cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="moe_block_dense_test",
        enable_checkpointing=False,
        model_name="mixtral-8x7b",
        dtype="float32",
        megablox=False,
        sparse_matmul=False,
        per_device_batch_size=4,
    )

    rng = jax.random.PRNGKey(2345)
    rng_model, rng_hidden_states = jax.random.split(rng)
    hidden_states = jax.random.uniform(
        rng_hidden_states, (int(cfg.per_device_batch_size), cfg.max_target_length, cfg.base_emb_dim), dtype=cfg.dtype
    )

    devices_array = maxtext_utils.create_device_mesh(cfg)
    mesh = Mesh(devices_array, cfg.mesh_axes)
    variables, expected_output = self.get_expected_output(rng_model, hidden_states, cfg)
    actual_output, _ = self.get_moe_output(variables, hidden_states, cfg, mesh)
    self.assertTrue(jax.numpy.allclose(expected_output, actual_output, rtol=1e-05, atol=1e-05, equal_nan=False))

  @pytest.mark.tpu_only
  def test_megablox_expert_parallelism(self):
    cfg = pyconfig.initialize(
        [None, os.path.join(PKG_DIR, "configs", "base.yml")],
        run_name="moe_block_megablox_ep_test",
        enable_checkpointing=False,
        model_name="mixtral-8x7b",
        dtype="bfloat16",
        megablox=True,
        sparse_matmul=True,
        per_device_batch_size=4,
        ici_expert_parallelism=4,
    )

    rng = jax.random.PRNGKey(2345)
    rng_model, rng_hidden_states = jax.random.split(rng)
    hidden_states = jax.random.uniform(
        rng_hidden_states, (int(cfg.per_device_batch_size), cfg.max_target_length, cfg.base_emb_dim), dtype=cfg.dtype
    )

    devices_array = maxtext_utils.create_device_mesh(cfg)
    mesh = Mesh(devices_array, cfg.mesh_axes)
    with nn_partitioning.axis_rules(cfg.logical_axis_rules):
      variables, expected_output = self.get_expected_output(rng_model, hidden_states, cfg)
      actual_output, _ = self.get_moe_output(variables, hidden_states, cfg, mesh)
      self.assertTrue(jax.numpy.allclose(expected_output, actual_output, rtol=1e-02, atol=1e-02, equal_nan=False))

  def test_random_routing(self):
    bs, seq_len, num_experts, num_experts_per_tok = 12, 1024, 8, 2
    rng = jax.random.PRNGKey(0)
    rng, logits_key = jax.random.split(rng)
    gate_logits = jax.random.normal(logits_key, (bs, seq_len, num_experts))

    rng, run_key = jax.random.split(rng)
    _, top_k_indices = moe.random_routing(run_key, gate_logits, num_experts_per_tok)

    flat_indices = top_k_indices.flatten()
    counts = jnp.bincount(flat_indices, length=num_experts)
    expected_count = bs * seq_len * num_experts_per_tok // num_experts
    tol = 0.05

    lower_bound = expected_count - expected_count * tol
    upper_bound = expected_count + expected_count * tol
    is_with_tolerance = (counts >= lower_bound) & (counts <= upper_bound)
    self.assertTrue(is_with_tolerance.all())


if __name__ == "__main__":
  unittest.main()
