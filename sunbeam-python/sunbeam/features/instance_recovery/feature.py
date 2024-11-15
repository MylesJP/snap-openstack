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

import click
from packaging.version import Version
from rich.console import Console

from sunbeam.core.common import BaseStep, RiskLevel, run_plan
from sunbeam.core.deployment import Deployment
from sunbeam.core.juju import JujuHelper
from sunbeam.core.manifest import (
    AddManifestStep,
    CharmManifest,
    FeatureConfig,
    SoftwareConfig,
)
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.core.terraform import TerraformInitStep
from sunbeam.features.interface.v1.openstack import (
    DisableOpenStackApplicationStep,
    EnableOpenStackApplicationStep,
    OpenStackControlPlaneFeature,
    TerraformPlanLocation,
)
from sunbeam.steps.hypervisor import ReapplyHypervisorTerraformPlanStep
from sunbeam.steps.juju import RemoveSaasApplicationsStep
from sunbeam.utils import click_option_show_hints, pass_method_obj
from sunbeam.versions import OPENSTACK_CHANNEL

from .consul import ConsulFeature

console = Console()


class InstanceRecoveryFeature(OpenStackControlPlaneFeature):
    version = Version("0.0.1")

    name = "instance-recovery"
    tf_plan_location = TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO

    risk_availability: RiskLevel = RiskLevel.EDGE

    def __init__(self):
        super().__init__()
        self.consul_feature = ConsulFeature()

    def default_software_overrides(self) -> SoftwareConfig:
        """Feature software configuration."""
        consul_default_overrides = self.consul_feature.default_software_overrides()
        instance_recovery_overrides = SoftwareConfig(
            charms={"masakari-k8s": CharmManifest(channel=OPENSTACK_CHANNEL)}
        )
        charm_overrides = (
            consul_default_overrides.charms | instance_recovery_overrides.charms
        )

        return SoftwareConfig(
            charms=charm_overrides, terraform=consul_default_overrides.terraform
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        consul_tfvar_map = self.consul_feature.manifest_attributes_tfvar_map()
        instance_recovery_tfvar_map = {
            self.tfplan: {
                "charms": {
                    "masakari-k8s": {
                        "channel": "masakari-channel",
                        "revision": "masakari-revision",
                        "config": "masakari-config",
                    }
                }
            }
        }

        return consul_tfvar_map | instance_recovery_tfvar_map

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
        """Run plans to enable consul and instance recovery features."""
        self.consul_feature.run_enable_plans(
            deployment=deployment, config=config, show_hints=show_hints
        )

        tfhelper = deployment.get_tfhelper(self.tfplan)
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
        """Run plans to disable the consul and instance recovery features."""
        tfhelper = deployment.get_tfhelper(self.tfplan)
        tfhelper_hypervisor = deployment.get_tfhelper("hypervisor-plan")
        jhelper = JujuHelper(deployment.get_connected_controller())
        extra_tfvars = {"masakari-offer-url": None}
        plan = [
            TerraformInitStep(tfhelper_hypervisor),
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
        self.consul_feature.run_disable_plans(
            deployment=deployment, show_hints=show_hints
        )
        click.echo(f"OpenStack {self.display_name} application disabled.")

    def set_application_names(self, deployment: Deployment) -> list:
        """Application names handled by the terraform plan."""
        consul_apps = self.consul_feature.set_application_names(deployment)

        instance_recovery_apps = ["masakari", "masakari-mysql-router"]
        if self.get_database_topology(deployment) == "multi":
            instance_recovery_apps.append("masakari-mysql")

        return instance_recovery_apps + consul_apps

    def set_tfvars_on_enable(
        self, deployment: Deployment, config: FeatureConfig
    ) -> dict:
        """Set terraform variables to enable the application."""
        consul_tfvars = self.consul_feature.set_tfvars_on_enable(
            deployment=deployment, config=config
        )
        instance_recovery_tfvars = {"enable-masakari": True}

        return consul_tfvars | instance_recovery_tfvars

    def set_tfvars_on_disable(self, deployment: Deployment) -> dict:
        """Set terraform variables to disable the application."""
        consul_tfvars = self.consul_feature.set_tfvars_on_disable(deployment=deployment)
        instance_recovery_tfvars = {"enable-masakari": False}

        return consul_tfvars | instance_recovery_tfvars

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
