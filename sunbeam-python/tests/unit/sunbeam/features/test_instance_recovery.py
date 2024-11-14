# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from sunbeam.core.common import ResultType
from sunbeam.core.deployment import Networks
from sunbeam.core.juju import TimeoutException
from sunbeam.core.terraform import TerraformException
from sunbeam.features.instance_recovery import feature as instance_recovery_feature


@pytest.fixture()
def tfhelper():
    yield Mock()


@pytest.fixture()
def jhelper():
    yield AsyncMock()


@pytest.fixture()
def deployment():
    yield Mock()


@pytest.fixture()
def consulfeature():
    with patch(
        "sunbeam.features.instance_recovery.feature.InstanceRecoveryFeature"
    ) as p:
        yield p


@pytest.fixture()
def update_config():
    with patch("sunbeam.features.instance_recovery.feature.update_config") as p:
        yield p


class TestDeployConsulClientStep:
    def test_run(self, deployment, tfhelper, jhelper, consulfeature):
        step = instance_recovery_feature.DeployConsulClientStep(
            deployment, consulfeature, tfhelper, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_apply_failed(self, deployment, tfhelper, jhelper, consulfeature):
        tfhelper.update_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed..."
        )
        step = instance_recovery_feature.DeployConsulClientStep(
            deployment, consulfeature, tfhelper, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "apply failed..."

    def test_run_waiting_timed_out(self, deployment, tfhelper, jhelper, consulfeature):
        jhelper.wait_until_desired_status.side_effect = TimeoutException("timed out")

        step = instance_recovery_feature.DeployConsulClientStep(
            deployment, consulfeature, tfhelper, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.update_tfvars_and_apply_tf.assert_called_once()
        jhelper.wait_until_desired_status.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestRemoveConsulClientStep:
    def test_run(self, deployment, tfhelper, jhelper, consulfeature, update_config):
        step = instance_recovery_feature.RemoveConsulClientStep(
            deployment, consulfeature, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_called_once()
        assert result.result_type == ResultType.COMPLETED

    def test_run_tf_destroy_failed(
        self, deployment, tfhelper, jhelper, consulfeature, update_config
    ):
        tfhelper.destroy.side_effect = TerraformException("destroy failed...")

        step = instance_recovery_feature.RemoveConsulClientStep(
            deployment, consulfeature, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_not_called()
        assert result.result_type == ResultType.FAILED
        assert result.message == "destroy failed..."

    def test_run_waiting_timed_out(
        self, deployment, tfhelper, jhelper, consulfeature, update_config
    ):
        jhelper.wait_application_gone.side_effect = TimeoutException("timed out")

        step = instance_recovery_feature.RemoveConsulClientStep(
            deployment, consulfeature, tfhelper, jhelper
        )
        result = step.run()

        tfhelper.destroy.assert_called_once()
        jhelper.wait_application_gone.assert_called_once()
        assert result.result_type == ResultType.FAILED
        assert result.message == "timed out"


class TestInstanceRecoveryFeature:
    @pytest.mark.parametrize(
        "spaces,expected_output",
        [
            (["mgmt", "mgmt", "mgmt"], [True, False, False]),
            (["mgmt", "data", "mgmt"], [True, True, False]),
            (["mgmt", "data", "storage"], [True, True, True]),
            (["mgmt", "mgmt", "storage"], [True, False, True]),
            (["mgmt", "data", "data"], [True, False, True]),
        ],
    )
    def test_set_tfvars_on_enable(self, deployment, snap, spaces, expected_output):
        def _get_space(network: Networks):
            if network == Networks.MANAGEMENT:
                return spaces[0]
            elif network == Networks.DATA:
                return spaces[1]
            elif network == Networks.STORAGE:
                return spaces[2]

            return spaces[0]

        deployment.get_space.side_effect = _get_space

        consul = instance_recovery_feature.InstanceRecoveryFeature()
        consul._manifest = MagicMock()
        feature_config = Mock()
        extra_tfvars = consul.set_tfvars_on_enable(deployment, feature_config)

        # Verify enable-consul-<> vars are set to true/false based on spaces defined
        for (
            index,
            server,
        ) in enumerate(
            [
                "enable-consul-management",
                "enable-consul-tenant",
                "enable-consul-storage",
            ]
        ):
            assert extra_tfvars.get(server) is expected_output[index]
