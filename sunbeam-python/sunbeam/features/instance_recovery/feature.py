# Copyright (c) 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import enum
import logging
from pathlib import Path
from typing import Any

import click
from packaging.version import Version
from rich.console import Console
from rich.status import Status

from sunbeam.core.common import (
    BaseStep,
    Result,
    ResultType,
    RiskLevel,
    run_plan,
    update_config,
    update_status_background,
)
from sunbeam.core.deployment import Deployment, Networks
from sunbeam.core.juju import JujuHelper, JujuWaitException, TimeoutException, run_sync
from sunbeam.core.manifest import (
    AddManifestStep,
    CharmManifest,
    FeatureConfig,
    SoftwareConfig,
    TerraformManifest,
)
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.core.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
)
from sunbeam.features.interface.v1.openstack import (
    DisableOpenStackApplicationStep,
    EnableOpenStackApplicationStep,
    OpenStackControlPlaneFeature,
    TerraformPlanLocation,
)
from sunbeam.steps.hypervisor import ReapplyHypervisorTerraformPlanStep
from sunbeam.steps.juju import RemoveSaasApplicationsStep
from sunbeam.utils import click_option_show_hints, pass_method_obj
from sunbeam.versions import CONSUL_CHANNEL, OPENSTACK_CHANNEL

LOG = logging.getLogger(__name__)
console = Console()

CONSUL_MANAGEMENT_SERF_LAN_PORT = 30301
CONSUL_TENANT_SERF_LAN_PORT = 30311
CONSUL_STORAGE_SERF_LAN_PORT = 30321
CONSUL_CLIENT_MANAGEMENT_SERF_LAN_PORT = 8301
CONSUL_CLIENT_TENANT_SERF_LAN_PORT = 8311
CONSUL_CLIENT_STORAGE_SERF_LAN_PORT = 8321

CONSUL_CLIENT_TFPLAN = "consul-client-plan"
CONSUL_CLIENT_CONFIG_KEY = "TerraformVarsFeatureConsulPlanConsulClient"
PRINCIPAL_APP = "openstack-hypervisor"


class ConsulServerNetworks(enum.Enum):
    MANAGEMENT = "management"
    TENANT = "tenant"
    STORAGE = "storage"


class DeployConsulClientStep(BaseStep):
    """Deploy Consul Client using Terraform."""

    _CONFIG = CONSUL_CLIENT_CONFIG_KEY

    def __init__(
        self,
        deployment: Deployment,
        feature: "InstanceRecoveryFeature",
        tfhelper: TerraformHelper,
        openstack_tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        app_desired_status: list[str] = ["active"],
    ):
        super().__init__("Deploy Consul Client", "Deploy Consul Client")
        self.deployment = deployment
        self.feature = feature
        self.tfhelper = tfhelper
        self.openstack_tfhelper = openstack_tfhelper
        self.jhelper = jhelper
        self.manifest = self.feature.manifest
        self.app_desired_status = app_desired_status
        self.client = self.deployment.get_client()
        self.model = self.deployment.openstack_machines_model

    def _get_tfvars(self) -> dict:
        """Construct tfvars for consul client."""
        openstack_backend_config = self.openstack_tfhelper.backend_config()

        tfvars: dict[str, Any] = {
            "principal-application-model": self.model,
            "principal-application": PRINCIPAL_APP,
            "openstack-state-backend": self.openstack_tfhelper.backend,
            "openstack-state-config": openstack_backend_config,
        }

        servers_to_enable = self.feature.consul_servers_to_enable(self.deployment)

        consul_config_map = {}
        consul_endpoint_bindings_map = {}
        if servers_to_enable.get(ConsulServerNetworks.MANAGEMENT):
            tfvars["enable-consul-management"] = True
            _management_config = {
                "serf-lan-port": CONSUL_CLIENT_MANAGEMENT_SERF_LAN_PORT,
            }
            _management_config.update(
                self.feature.get_config_from_manifest(
                    "consul-client", ConsulServerNetworks.MANAGEMENT
                )
            )
            consul_config_map["consul-management"] = _management_config
            consul_endpoint_bindings_map["consul-management"] = [
                {"space": self.deployment.get_space(Networks.MANAGEMENT)},
                {
                    "endpoint": "consul",
                    "space": self.deployment.get_space(Networks.MANAGEMENT),
                },
            ]
        else:
            tfvars["enable-consul-management"] = False

        if servers_to_enable.get(ConsulServerNetworks.TENANT):
            tfvars["enable-consul-tenant"] = True
            _tenant_config = {
                "serf-lan-port": CONSUL_CLIENT_TENANT_SERF_LAN_PORT,
            }
            _tenant_config.update(
                self.feature.get_config_from_manifest(
                    "consul-client", ConsulServerNetworks.TENANT
                )
            )
            consul_config_map["consul-tenant"] = _tenant_config
            consul_endpoint_bindings_map["consul-tenant"] = [
                {"space": self.deployment.get_space(Networks.MANAGEMENT)},
                {
                    "endpoint": "consul",
                    "space": self.deployment.get_space(Networks.DATA),
                },
            ]
        else:
            tfvars["enable-consul-tenant"] = False

        if servers_to_enable.get(ConsulServerNetworks.STORAGE):
            tfvars["enable-consul-storage"] = True
            _storage_config = {
                "serf-lan-port": CONSUL_CLIENT_STORAGE_SERF_LAN_PORT,
            }
            _storage_config.update(
                self.feature.get_config_from_manifest(
                    "consul-client", ConsulServerNetworks.STORAGE
                )
            )
            consul_config_map["consul-storage"] = _storage_config
            consul_endpoint_bindings_map["consul-storage"] = [
                {"space": self.deployment.get_space(Networks.MANAGEMENT)},
                {
                    "endpoint": "consul",
                    "space": self.deployment.get_space(Networks.STORAGE),
                },
            ]
        else:
            tfvars["enable-consul-storage"] = False

        tfvars["consul-config-map"] = consul_config_map
        tfvars["consul-endpoint-bindings-map"] = consul_endpoint_bindings_map
        return tfvars

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        extra_tfvars = self._get_tfvars()
        try:
            self.update_status(status, "deploying services")
            self.tfhelper.update_tfvars_and_apply_tf(
                self.client,
                self.manifest,
                tfvar_config=self._CONFIG,
                override_tfvars=extra_tfvars,
            )
        except TerraformException as e:
            LOG.exception("Error deploying consul client")
            return Result(ResultType.FAILED, str(e))

        apps = self.feature.set_consul_client_application_names(self.deployment)
        LOG.debug(f"Application monitored for readiness: {apps}")
        queue: asyncio.queues.Queue[str] = asyncio.queues.Queue(maxsize=len(apps))
        task = run_sync(update_status_background(self, apps, queue, status))
        try:
            run_sync(
                self.jhelper.wait_until_desired_status(
                    self.model,
                    apps,
                    status=self.app_desired_status,
                    timeout=self.feature.set_application_timeout_on_enable(),
                    queue=queue,
                )
            )
        except (JujuWaitException, TimeoutException) as e:
            LOG.debug("Failed to deploy consul client", exc_info=True)
            return Result(ResultType.FAILED, str(e))
        finally:
            if not task.done():
                task.cancel()

        return Result(ResultType.COMPLETED)


class RemoveConsulClientStep(BaseStep):
    """Remove Consul Client using Terraform."""

    _CONFIG = CONSUL_CLIENT_CONFIG_KEY

    def __init__(
        self,
        deployment: Deployment,
        feature: "InstanceRecoveryFeature",
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
    ):
        super().__init__("Remove Consul Client", "Removing Consul Client")
        self.deployment = deployment
        self.feature = feature
        self.tfhelper = tfhelper
        self.jhelper = jhelper
        self.manifest = self.feature.manifest
        self.client = deployment.get_client()
        self.model = deployment.openstack_machines_model

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        try:
            self.tfhelper.destroy()
        except TerraformException as e:
            LOG.exception("Error destroying consul client")
            return Result(ResultType.FAILED, str(e))

        apps = self.feature.set_consul_client_application_names(self.deployment)
        LOG.debug(f"Application monitored for removal: {apps}")
        try:
            run_sync(
                self.jhelper.wait_application_gone(
                    apps,
                    self.model,
                    timeout=self.feature.set_application_timeout_on_disable(),
                )
            )
        except TimeoutException as e:
            LOG.debug(f"Failed to destroy {apps}", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        extra_tfvars = self.feature.set_tfvars_on_disable(self.deployment)
        update_config(self.client, self._CONFIG, extra_tfvars)

        return Result(ResultType.COMPLETED)


class InstanceRecoveryFeature(OpenStackControlPlaneFeature):
    version = Version("0.0.1")

    # requires = {FeatureRequirement("consul")}
    name = "instance-recovery"
    tf_plan_location = TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO

    risk_availability: RiskLevel = RiskLevel.EDGE

    def __init__(self) -> None:
        super().__init__()
        self.tfplan_consul_client = CONSUL_CLIENT_TFPLAN
        self.tfplan_consul_client_dir = "deploy-consul-client"

    def default_software_overrides(self) -> SoftwareConfig:
        """Feature software configuration."""
        return SoftwareConfig(
            charms={
                "masakari-k8s": CharmManifest(channel=OPENSTACK_CHANNEL),
                "consul-k8s": CharmManifest(channel=CONSUL_CHANNEL),
                "consul-client": CharmManifest(channel=CONSUL_CHANNEL),
            },
            terraform={
                self.tfplan_consul_client: TerraformManifest(
                    source=Path(__file__).parent
                    / "etc"  # noqa: W503
                    / self.tfplan_consul_client_dir  # noqa: W503
                ),
            },
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan: {
                "charms": {
                    "masakari-k8s": {
                        "channel": "masakari-channel",
                        "revision": "masakari-revision",
                        "config": "masakari-config",
                    },
                    "consul-k8s": {
                        "channel": "consul-channel",
                        "revision": "consul-revision",
                        "config": "consul-config",
                        "config-map": "consul-config-map",
                    },
                }
            },
            self.tfplan_consul_client: {
                "charms": {
                    "consul-client": {
                        "channel": "consul-channel",
                        "revision": "consul-revision",
                        "config": "consul-config",
                        "config-map": "consul-config-map",
                    }
                }
            },
        }

    def pre_enable(
        self, deployment: Deployment, config: FeatureConfig, show_hints: bool
    ) -> None:
        """Handler to perform tasks before enabling the feature."""
        if self.get_cluster_topology(deployment) == "single":
            click.echo("WARNING: This feature is meant for multi-node deployment only.")

        super().pre_enable(deployment, config, show_hints)

    def run_enable_plans(
        self, deployment: Deployment, config: FeatureConfig, show_hints: bool
    ) -> None:
        """Run plans to enable feature."""
        tfhelper = deployment.get_tfhelper(self.tfplan)
        tfhelper_consul_client = deployment.get_tfhelper(self.tfplan_consul_client)
        tfhelper_openstack = deployment.get_tfhelper("openstack-plan")
        tfhelper_hypervisor = deployment.get_tfhelper("hypervisor-plan")
        jhelper = JujuHelper(deployment.get_connected_controller())
        plan1: list[BaseStep] = []
        if self.user_manifest:
            plan1.append(AddManifestStep(deployment.get_client(), self.user_manifest))
        plan1.extend(
            [
                TerraformInitStep(tfhelper),
                EnableOpenStackApplicationStep(
                    deployment, config, tfhelper, jhelper, self
                ),
                TerraformInitStep(tfhelper_consul_client),
                DeployConsulClientStep(
                    deployment,
                    self,
                    tfhelper_consul_client,
                    tfhelper,
                    jhelper,
                ),
            ]
        )
        run_plan(plan1, console, show_hints)

        openstack_tf_output = tfhelper_openstack.output()
        extra_tfvars = {
            "masakari-offer-url": openstack_tf_output.get("masakari-offer-url")
        }
        plan2: list[BaseStep] = []
        plan2.extend(
            [
                TerraformInitStep(tfhelper_hypervisor),
                ReapplyHypervisorTerraformPlanStep(
                    deployment.get_client(),
                    tfhelper_hypervisor,
                    jhelper,
                    self.manifest,
                    deployment.openstack_machines_model,
                    extra_tfvars=extra_tfvars,
                ),
            ]
        )
        run_plan(plan2, console, show_hints)
        click.echo(f"OpenStack {self.display_name} application enabled.")

    def run_disable_plans(self, deployment: Deployment, show_hints: bool) -> None:
        """Run plans to disable the feature."""
        tfhelper = deployment.get_tfhelper(self.tfplan)
        tfhelper_consul_client = deployment.get_tfhelper(self.tfplan_consul_client)
        tfhelper_hypervisor = deployment.get_tfhelper("hypervisor-plan")
        jhelper = JujuHelper(deployment.get_connected_controller())
        extra_tfvars = {"masakari-offer-url": None}
        plan = [
            TerraformInitStep(tfhelper_hypervisor),
            TerraformInitStep(tfhelper_consul_client),
            RemoveConsulClientStep(deployment, self, tfhelper_consul_client, jhelper),
            ReapplyHypervisorTerraformPlanStep(
                deployment.get_client(),
                tfhelper_hypervisor,
                jhelper,
                self.manifest,
                deployment.openstack_machines_model,
                extra_tfvars=extra_tfvars,
            ),
            RemoveSaasApplicationsStep(
                jhelper,
                deployment.openstack_machines_model,
                OPENSTACK_MODEL,
                saas_apps_to_delete=["masakari"],
            ),
            TerraformInitStep(tfhelper),
            DisableOpenStackApplicationStep(deployment, tfhelper, jhelper, self),
        ]

        run_plan(plan, console, show_hints)
        click.echo(f"OpenStack {self.display_name} application disabled.")

    def consul_servers_to_enable(
        self, deployment: Deployment
    ) -> dict[ConsulServerNetworks, bool]:
        """Return consul servers to enable.

        Return dict to enable/disable consul server per network.
        """
        # Default to false
        enable = dict.fromkeys(ConsulServerNetworks, False)

        try:
            management_space = deployment.get_space(Networks.MANAGEMENT)
            enable[ConsulServerNetworks.MANAGEMENT] = True
        except ValueError:
            management_space = None

        # If storage space is same as management space, dont enable consul
        # server for storage
        try:
            storage_space = deployment.get_space(Networks.STORAGE)
            if storage_space != management_space:
                enable[ConsulServerNetworks.STORAGE] = True
        except ValueError:
            storage_space = None

        # If data space is same as either of management or storage space,
        # dont enable consul server for tenant
        try:
            tenant_space = deployment.get_space(Networks.DATA)
            if tenant_space not in (management_space, storage_space):
                enable[ConsulServerNetworks.TENANT] = True
        except ValueError:
            tenant_space = None

        return enable

    def get_config_from_manifest(
        self, charm: str, network: ConsulServerNetworks
    ) -> dict:
        """Compute config from manifest.

        Compute config from manifest based on sections config and config-map.
        config-map holds consul configs for each ConsulServerNetworks.
        config-map takes precedence over config section.
        """
        feature_manifest = self.manifest.get_feature(self.name)
        if not feature_manifest:
            return {}

        charm_manifest = feature_manifest.software.charms.get(charm)
        if not charm_manifest:
            return {}

        config = {}
        # Read feature.consul.software.charms.consul-k8s.config
        if charm_manifest.config:
            config.update(charm_manifest.config)

        # Read feature-consul.software.charms.consul-k8s.config-map
        # config-map is an extra field for CharmManifest, so use model_extra
        if charm_manifest.model_extra:
            config.update(
                charm_manifest.model_extra.get("config-map", {}).get(
                    f"consul-{network.value}", {}
                )
            )

        return config

    def set_application_names(self, deployment: Deployment) -> list:
        """Application names handled by the terraform plan."""
        apps = [
            "masakari",
            "masakari-mysql-router",
            *[
                f"consul-{k.value}"
                for k, v in self.consul_servers_to_enable(deployment).items()
                if v
            ],
        ]
        if self.get_database_topology(deployment) == "multi":
            apps.append("masakari-mysql")

        return apps

    def set_consul_client_application_names(self, deployment: Deployment) -> list:
        """Application names handled by the consul client terraform plan."""
        enable = [
            f"consul-client-{k.value}"
            for k, v in self.consul_servers_to_enable(deployment).items()
            if v
        ]
        return enable

    def set_tfvars_on_enable(
        self, deployment: Deployment, config: FeatureConfig
    ) -> dict:
        """Set terraform variables to enable the application."""
        tfvars: dict[str, Any] = {}
        servers_to_enable = self.consul_servers_to_enable(deployment)

        consul_config_map = {}

        if servers_to_enable.get(ConsulServerNetworks.MANAGEMENT):
            tfvars["enable-consul-management"] = True
            _management_config = {
                "expose-gossip-and-rpc-ports": True,
                "serflan-node-port": CONSUL_TENANT_SERF_LAN_PORT,
            }
            # Manifest takes precedence
            _management_config.update(
                self.get_config_from_manifest(
                    "consul-k8s", ConsulServerNetworks.MANAGEMENT
                )
            )
            consul_config_map["consul-management"] = _management_config
        else:
            tfvars["enable-consul-management"] = False

        if servers_to_enable.get(ConsulServerNetworks.TENANT):
            tfvars["enable-consul-tenant"] = True
            _tenant_config = {
                "expose-gossip-and-rpc-ports": True,
                "serflan-node-port": CONSUL_TENANT_SERF_LAN_PORT,
            }
            # Manifest takes precedence
            _tenant_config.update(
                self.get_config_from_manifest("consul-k8s", ConsulServerNetworks.TENANT)
            )
            consul_config_map["consul-tenant"] = _tenant_config
        else:
            tfvars["enable-consul-tenant"] = False

        if servers_to_enable.get(ConsulServerNetworks.STORAGE):
            tfvars["enable-consul-storage"] = True
            _storage_config = {
                "expose-gossip-and-rpc-ports": True,
                "serflan-node-port": CONSUL_TENANT_SERF_LAN_PORT,
            }
            # Manifest takes precedence
            _storage_config.update(
                self.get_config_from_manifest(
                    "consul-k8s", ConsulServerNetworks.STORAGE
                )
            )
            consul_config_map["consul-storage"] = _storage_config
        else:
            tfvars["enable-consul-storage"] = False

        tfvars["consul-config-map"] = consul_config_map
        tfvars["enable-masakari"] = True
        return tfvars

    def set_tfvars_on_disable(self, deployment: Deployment) -> dict:
        """Set terraform variables to disable the application."""
        return {
            "enable-masakari": False,
            "enable-consul-management": False,
            "enable-consul-tenant": False,
            "enable-consul-storage": False,
        }

    def set_tfvars_on_resize(
        self, deployment: Deployment, config: FeatureConfig
    ) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def get_database_charm_processes(self) -> dict[str, dict[str, int]]:
        """Returns the database processes accessing this service."""
        return {
            "masakari": {"masakari-k8s": 8},
        }

    @click.command()
    @click_option_show_hints
    @pass_method_obj
    def enable_cmd(self, deployment: Deployment, show_hints: bool) -> None:
        """Enable OpenStack Instance Recovery service."""
        self.enable_feature(deployment, FeatureConfig(), show_hints)

    @click.command()
    @click_option_show_hints
    @pass_method_obj
    def disable_cmd(self, deployment: Deployment, show_hints: bool) -> None:
        """Disable OpenStack Instance Recovery service."""
        self.disable_feature(deployment, show_hints)
