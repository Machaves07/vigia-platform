"""Pila ``vigia-edge``: zona alojada, certificados públicos, balanceadores, grupos de destino,
almacén de confianza y cortafuegos (§4). Depende de ``vigia-foundation`` y de ``vigia-data``
(depósito ``vigia-edge``). Recursos: TASK-147.

Dominio y certificados (§4.1 y tabla D-8 de §2.1):

- ``pilot`` aloja la zona ``vigia-zone`` del dominio que el dueño registra en el parámetro SSM
  ``/vigia/domain`` (P5) y publica su identificador y su nombre en ``/vigia/<despliegue>/zone-id`` y
  ``zone-name`` (contrato de salidas nº 38). ``staging-<n>`` **importa** esa zona por atributos y
  solo crea, y borra con el entorno, ``staging-<n>.<dominio>`` y ``staging-<n>-nodes.<dominio>``.
- Un certificado público por nombre con validación por DNS en la zona; política TLS 1.2 y 1.3
  (``ELBSecurityPolicy-TLS13-1-2-2021-06``).

``vigia-alb-app`` (§4.2, nº 15 y nota U02-H-07 de §4.3): público, ``sg-alb-app``, solo 443, registro
de acceso en ``vigia-logs`` bajo ``alb/app/``. Reglas, en orden de evaluación:

1. Petición con alguna cabecera ``X-Amzn-Mtls-*`` → ``400`` fijo. El balanceador de aplicación
   no puede quitar una cabecera arbitraria, así que la rechaza: nadie la falsifica hacia la
   aplicación por ``app.``.
2. Exactamente ``/api/nodes/enrollment`` → ``tg-api`` (alta del nodo, nº 15).
3. ``/api/nodes`` y ``/api/nodes/*`` → ``404`` fijo: los nodos solo entran por ``nodes.``.
4. ``/health/ready`` → ``404`` fijo: la salud profunda solo la ve el grupo de destino desde la VPC.
5. Lo demás → ``tg-api`` (HTTP 8000, salud ``GET /health/ready``, drenaje de 30 s).

``vigia-app-waf`` (§4.4, solo donde ``waf_enabled``: ``pilot``): tasa de la alta de 20 por dirección
cada 10 min (nº 15); tasa general de 6 000 por dirección cada 5 min con aviso de bloqueos a
``vigia-alerts`` (nº 11); reputación de IP, conjunto común y entradas malas conocidas. La regla
``SizeRestrictions_BODY`` del conjunto común pasa a contar sin bloquear: con un balanceador de
aplicación el cortafuegos solo inspecciona los primeros 8 KB del cuerpo, así que ningún límite
explícito por encima de 8 KB es posible y un borrador de 8 000 caracteres de U-05 los supera
(hipótesis (c) de R14). El límite de tamaño del cuerpo queda en la aplicación. Registro en
``aws-waf-logs-vigia-app`` (90 días, ``vigia-logs``).

Nodos (§4.3 y R2), según ``nodes_tls_mode``:

- ``mtls``: ``vigia-alb-nodes`` con ``sg-alb-nodes``, grupo propio ``tg-api-nodes`` (HTTP 8000,
  salud ``GET /health/live``; hipótesis (g) de R14) y escucha 443 con autenticación mutua en modo de
  verificación contra ``vigia-node-trust`` (``ca/root.pem`` de ``vigia-edge``), que solo reenvía
  ``/api/nodes/*`` y responde ``404`` fijo a lo demás. La lista de revocación la añade al almacén el
  worker (A-19), no esta pila. Con ``first_deploy=true`` (paso 3 del primer despliegue) no se
  sintetizan ni el almacén ni la escucha, porque la raíz aún no existe; el paso 8 despliega de
  nuevo con ``first_deploy=false``. Mientras tanto ``tg-api-nodes`` no está asociado a ningún
  balanceador.
- ``passthrough`` (contingencia de R2): balanceador de red ``vigia-nlb-nodes`` con el mismo grupo
  ``sg-alb-nodes``, escucha TCP 443 con paso directo hacia ``tg-api-nodes`` (TCP 8443, donde la
  aplicación termina TLS con su certificado y verifica el del nodo). Sin almacén de confianza ni
  certificado público de ``nodes.``. De las demás pilas solo cambia ``vigia-compute``, que expone
  el puerto 8443 en la tarea de ``vigia-api`` (TASK-148).

Ningún balanceador escucha en 80 y todos registran el acceso; ``sg-api`` recibe 8000 (o 8443 en
la contingencia) solo desde el grupo de su balanceador (§3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aws_cdk import ArnFormat, Duration, Fn, RemovalPolicy
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from config import EnvironmentConfig, NodesTlsMode
from stacks.base import VigiaStack
from stacks.data import (
    DOMAIN_PARAMETER,
    LOAD_BALANCER_LOG_PREFIXES,
    LOGS_USAGE,
    BucketUsage,
    app_host,
)
from stacks.foundation import (
    PUBLIC_SUBNETS,
    FoundationStack,
    KeyName,
    SecurityGroupName,
    waf_log_group_name,
)
from stacks.outputs import Output, import_hosted_zone, publish

# --- Dominio y certificados (§4.1) --------------------------------------------------------

ZONE_COMMENT = "vigia-zone (infrastructure-design 4.1)"
PILOT_NODES_HOST = "nodes"
# Nombre literal de §4.1: con ``usePostQuantumTlsPolicy`` en ``cdk.json``, ``RECOMMENDED_TLS``
# sintetizaría otra política, así que se fija en la escucha de nivel 1.
TLS_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"

# --- Balanceadores (§4.2 y §4.3) ----------------------------------------------------------

HTTPS_PORT = 443
API_PORT = 8000
# Contingencia de R2: puerto TLS de ``vigia-api`` hacia el que pasa el tráfico sin terminar
# ``[hipótesis; lo fija la definición de tarea de TASK-148]``.
PASSTHROUGH_PORT = 8443
READY_PATH = "/health/ready"
LIVE_PATH = "/health/live"
HEALTH_INTERVAL = Duration.seconds(30)
HEALTH_TIMEOUT = Duration.seconds(5)
HEALTHY_THRESHOLD = 2
UNHEALTHY_THRESHOLD = 2
DEREGISTRATION_DELAY = Duration.seconds(30)
APP_LOG_PREFIX, NODES_LOG_PREFIX = LOAD_BALANCER_LOG_PREFIXES
ROOT_CERTIFICATE_KEY = "ca/root.pem"

ENROLLMENT_PATH = "/api/nodes/enrollment"
NODES_PATHS = ("/api/nodes", "/api/nodes/*")
READY_PATHS = (READY_PATH, f"{READY_PATH}/*")
# Cabeceras que el balanceador añade con autenticación mutua (verificación y paso directo): por
# ``app.`` nadie las envía legítimamente.
MTLS_HEADERS = (
    "X-Amzn-Mtls-Clientcert-Subject",
    "X-Amzn-Mtls-Clientcert-Serial-Number",
    "X-Amzn-Mtls-Clientcert-Validity",
    "X-Amzn-Mtls-Clientcert-Issuer",
    "X-Amzn-Mtls-Clientcert-Leaf",
    "X-Amzn-Mtls-Clientcert",
)
# Prioridades de las reglas de ``vigia-alb-app``: menor se evalúa antes.
MTLS_HEADER_PRIORITY = 1  # 1 a 6, una por cabecera
ENROLLMENT_PRIORITY = 10
NODES_NOT_FOUND_PRIORITY = 20
READY_NOT_FOUND_PRIORITY = 30
# Regla única de ``vigia-alb-nodes``.
NODES_FORWARD_PRIORITY = 10
NOT_FOUND = 404
REJECTED_HEADER = 400

# --- Cortafuegos (§4.4) -------------------------------------------------------------------

WAF_SCOPE = "REGIONAL"
GENERAL_RATE_LIMIT = 6000  # [objetivo propio], nº 11
GENERAL_RATE_WINDOW_SECONDS = 300
ENROLLMENT_RATE_LIMIT = 20  # [objetivo propio], nº 15
ENROLLMENT_RATE_WINDOW_SECONDS = 600
ENROLLMENT_RATE_RULE = "enrollment-rate"
GENERAL_RATE_RULE = "general-rate"
# Grupos gestionados, en su orden de evaluación, con la regla que cuenta sin bloquear.
MANAGED_RULE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AWSManagedRulesAmazonIpReputationList", ()),
    ("AWSManagedRulesCommonRuleSet", ("SizeRestrictions_BODY",)),
    ("AWSManagedRulesKnownBadInputsRuleSet", ()),
)
WAF_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS  # 90 días
# Aviso de bloqueos de la regla de tasa general (nº 11): cualquier bloqueo en 5 minutos.
RATE_ALARM_PERIOD = Duration.minutes(5)
RATE_ALARM_THRESHOLD = 1


def nodes_host(config: EnvironmentConfig) -> str:
    """Nombre de los nodos: ``nodes`` en ``pilot``, ``staging-<n>-nodes`` en staging (§4.1)."""
    return f"{config.environment}-nodes" if config.ephemeral else PILOT_NODES_HOST


def target_group_name(config: EnvironmentConfig, name: str) -> str:
    """``tg-api`` y ``tg-api-nodes`` (§4.2 y nota U02-H-07) con el sufijo del despliegue."""
    return f"{name}{config.name_suffix}"


class EdgeStack(VigiaStack):
    """Pila ``vigia-edge`` (infrastructure-design §2.3)."""

    key = "edge"
    summary = "zona, certificados, balanceadores, almacen de confianza, cortafuegos"

    def __init__(
        self, scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
    ) -> None:
        super().__init__(scope, config, tags=tags)
        foundation = scope.node.find_child(config.stack_name(FoundationStack.key))
        if not isinstance(foundation, FoundationStack):
            raise TypeError("vigia-edge necesita vigia-foundation registrada antes")
        self.foundation = foundation
        self.zone = self._zone()
        # Nombres y certificados cuelgan del nombre de la zona: el de ``/vigia/domain`` en
        # ``pilot`` y el que publicó ``pilot`` en ``staging``.
        self.domain = self.zone.zone_name
        # Por nombre: esta pila no toca la política del depósito (la concede su dueño).
        self.access_logs_bucket = s3.Bucket.from_bucket_name(
            self, "AccessLogsBucket", config.bucket_name(LOGS_USAGE, self.account)
        )
        self.security_groups = self._security_groups()

        self.app_target_group = self._app_target_group()
        self.app_load_balancer = self._app_load_balancer()
        self.app_listener = self._app_listener()
        self._record("AppRecord", app_host(config), self.app_load_balancer)

        self.trust_store: elbv2.TrustStore | None = None
        self.nodes_listener: elbv2.ApplicationListener | elbv2.NetworkListener | None = None
        self.nodes_target_group: elbv2.ApplicationTargetGroup | elbv2.NetworkTargetGroup
        self.nodes_load_balancer: elbv2.ApplicationLoadBalancer | elbv2.NetworkLoadBalancer
        if config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH:
            self._nodes_passthrough()
        else:
            self._nodes_mtls()
        self._record("NodesRecord", nodes_host(config), self.nodes_load_balancer)

        self.web_acl = self._web_acl() if config.waf_enabled else None

    # --- Zona y certificados --------------------------------------------------------------

    def _zone(self) -> route53.IHostedZone:
        """``vigia-zone`` alojada en ``pilot``; importada por atributos en ``staging`` (D-8)."""
        config = self.config
        if not config.hosted_zone_owned:
            return import_hosted_zone(self, "Zone", config)
        domain = ssm.StringParameter.value_for_string_parameter(self, DOMAIN_PARAMETER)
        zone = route53.PublicHostedZone(self, "Zone", zone_name=domain, comment=ZONE_COMMENT)
        # La delegación del registrador apunta a sus servidores: no se recrea con la pila.
        zone.apply_removal_policy(RemovalPolicy.RETAIN)
        publish(self, config, Output.HOSTED_ZONE_ID, zone.hosted_zone_id)
        publish(self, config, Output.HOSTED_ZONE_NAME, domain)
        return zone

    def _fqdn(self, host: str) -> str:
        return Fn.join(".", [host, self.domain])

    def _certificate(self, construct_id: str, host: str) -> acm.Certificate:
        return acm.Certificate(
            self,
            construct_id,
            domain_name=self._fqdn(host),
            validation=acm.CertificateValidation.from_dns(self.zone),
        )

    def _record(
        self, construct_id: str, host: str, load_balancer: elbv2.ILoadBalancerV2
    ) -> route53.ARecord:
        return route53.ARecord(
            self,
            construct_id,
            zone=self.zone,
            # Relativo: CDK le añade el nombre de la zona.
            record_name=host,
            target=route53.RecordTarget.from_alias(
                route53_targets.LoadBalancerTarget(load_balancer)
            ),
        )

    # --- Grupos de seguridad (§3) ---------------------------------------------------------

    def _security_groups(self) -> dict[str, ec2.SecurityGroup]:
        """``sg-alb-app`` y ``sg-alb-nodes``: 443 desde Internet y salida solo hacia ``sg-api``."""
        api = self.foundation.security_groups[SecurityGroupName.API]
        nodes_port = (
            PASSTHROUGH_PORT if self.config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH else API_PORT
        )
        groups: dict[str, ec2.SecurityGroup] = {}
        for name, port in (("sg-alb-app", API_PORT), ("sg-alb-nodes", nodes_port)):
            group = ec2.SecurityGroup(
                self,
                f"SecurityGroup-{name}",
                vpc=self.foundation.vpc,
                security_group_name=name,
                description=f"{name} (infrastructure-design 3)",
                allow_all_outbound=False,
            )
            group.add_ingress_rule(
                ec2.Peer.any_ipv4(), ec2.Port.tcp(HTTPS_PORT), "443 desde Internet"
            )
            group.add_egress_rule(api, ec2.Port.tcp(port), f"{port} hacia sg-api")
            # En esta pila, no en ``vigia-foundation``: la regla nace y muere con el balanceador.
            ec2.CfnSecurityGroupIngress(
                self,
                f"ApiIngressFrom-{name}",
                group_id=api.security_group_id,
                source_security_group_id=group.security_group_id,
                ip_protocol="tcp",
                from_port=port,
                to_port=port,
                description=f"{port} desde {name}",
            )
            groups[name] = group
        return groups

    def _public_subnets(self) -> ec2.SubnetSelection:
        return ec2.SubnetSelection(subnet_group_name=PUBLIC_SUBNETS)

    # --- Balanceador de personas (§4.2) ---------------------------------------------------

    def _app_target_group(self) -> elbv2.ApplicationTargetGroup:
        return self._http_target_group("ApiTargetGroup", "tg-api", READY_PATH)

    def _http_target_group(
        self, construct_id: str, name: str, health_path: str
    ) -> elbv2.ApplicationTargetGroup:
        """Grupo de las tareas de ``vigia-api`` (las registra ``vigia-compute``, TASK-148)."""
        return elbv2.ApplicationTargetGroup(
            self,
            construct_id,
            target_group_name=target_group_name(self.config, name),
            vpc=self.foundation.vpc,
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.ApplicationProtocol.HTTP,
            port=API_PORT,
            deregistration_delay=DEREGISTRATION_DELAY,
            health_check=elbv2.HealthCheck(
                path=health_path,
                protocol=elbv2.Protocol.HTTP,
                interval=HEALTH_INTERVAL,
                timeout=HEALTH_TIMEOUT,
                healthy_threshold_count=HEALTHY_THRESHOLD,
                unhealthy_threshold_count=UNHEALTHY_THRESHOLD,
                healthy_http_codes="200",
            ),
        )

    def _application_load_balancer(
        self, construct_id: str, name: str, group: str, log_prefix: str
    ) -> elbv2.ApplicationLoadBalancer:
        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            construct_id,
            load_balancer_name=self.config.resource_name(name),
            vpc=self.foundation.vpc,
            vpc_subnets=self._public_subnets(),
            internet_facing=True,
            security_group=self.security_groups[group],
            # ``staging-<n>`` se destruye entero (D-8).
            deletion_protection=not self.config.ephemeral,
            drop_invalid_header_fields=True,
        )
        load_balancer.log_access_logs(self.access_logs_bucket, prefix=log_prefix)
        return load_balancer

    def _app_load_balancer(self) -> elbv2.ApplicationLoadBalancer:
        return self._application_load_balancer(
            "AppLoadBalancer", "alb-app", "sg-alb-app", APP_LOG_PREFIX
        )

    def _https_listener(
        self,
        load_balancer: elbv2.ApplicationLoadBalancer,
        certificate: acm.ICertificate,
        default_action: elbv2.ListenerAction,
        mutual_authentication: elbv2.MutualAuthentication | None = None,
    ) -> elbv2.ApplicationListener:
        listener = load_balancer.add_listener(
            "Https",
            port=HTTPS_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[elbv2.ListenerCertificate.from_certificate_manager(certificate)],
            default_action=default_action,
            mutual_authentication=mutual_authentication,
            # La entrada 443 ya está en el grupo del balanceador (``_security_groups``).
            open=False,
        )
        cfn_listener = listener.node.default_child
        if not isinstance(cfn_listener, elbv2.CfnListener):
            raise TypeError("la escucha HTTPS no tiene su recurso de nivel 1")
        cfn_listener.ssl_policy = TLS_POLICY
        return listener

    def _app_listener(self) -> elbv2.ApplicationListener:
        certificate = self._certificate("AppCertificate", app_host(self.config))
        listener = self._https_listener(
            self.app_load_balancer,
            certificate,
            elbv2.ListenerAction.forward([self.app_target_group]),
        )
        for offset, header in enumerate(MTLS_HEADERS):
            self._rule(
                listener,
                f"AppRejects-{header}",
                MTLS_HEADER_PRIORITY + offset,
                [elbv2.ListenerCondition.http_header(header, ["*"])],
                self._fixed(REJECTED_HEADER),
            )
        self._rule(
            listener,
            "AppEnrollment",
            ENROLLMENT_PRIORITY,
            [elbv2.ListenerCondition.path_patterns([ENROLLMENT_PATH])],
            elbv2.ListenerAction.forward([self.app_target_group]),
        )
        self._rule(
            listener,
            "AppNodesNotFound",
            NODES_NOT_FOUND_PRIORITY,
            [elbv2.ListenerCondition.path_patterns(list(NODES_PATHS))],
            self._fixed(NOT_FOUND),
        )
        self._rule(
            listener,
            "AppReadyNotFound",
            READY_NOT_FOUND_PRIORITY,
            [elbv2.ListenerCondition.path_patterns(list(READY_PATHS))],
            self._fixed(NOT_FOUND),
        )
        return listener

    @staticmethod
    def _fixed(status: int) -> elbv2.ListenerAction:
        return elbv2.ListenerAction.fixed_response(status, content_type="text/plain")

    def _rule(
        self,
        listener: elbv2.ApplicationListener,
        construct_id: str,
        priority: int,
        conditions: Sequence[elbv2.ListenerCondition],
        action: elbv2.ListenerAction,
    ) -> elbv2.ApplicationListenerRule:
        return elbv2.ApplicationListenerRule(
            self,
            construct_id,
            listener=listener,
            priority=priority,
            conditions=list(conditions),
            action=action,
        )

    # --- Balanceador de nodos (§4.3) ------------------------------------------------------

    def _nodes_mtls(self) -> None:
        """Autenticación mutua en el balanceador; sin almacén ni escucha en ``first_deploy``."""
        config = self.config
        self.nodes_target_group = self._http_target_group(
            "ApiNodesTargetGroup", "tg-api-nodes", LIVE_PATH
        )
        load_balancer = self._application_load_balancer(
            "NodesLoadBalancer", "alb-nodes", "sg-alb-nodes", NODES_LOG_PREFIX
        )
        self.nodes_load_balancer = load_balancer
        certificate = self._certificate("NodesCertificate", nodes_host(config))
        if config.first_deploy:
            return
        edge_bucket = s3.Bucket.from_bucket_name(
            self, "EdgeBucket", config.bucket_name(BucketUsage.EDGE.value, self.account)
        )
        self.trust_store = elbv2.TrustStore(
            self,
            "NodeTrust",
            trust_store_name=config.resource_name("node-trust"),
            bucket=edge_bucket,
            key=ROOT_CERTIFICATE_KEY,
        )
        listener = self._https_listener(
            load_balancer,
            certificate,
            self._fixed(NOT_FOUND),
            elbv2.MutualAuthentication(
                mutual_authentication_mode=elbv2.MutualAuthenticationMode.VERIFY,
                trust_store=self.trust_store,
                ignore_client_certificate_expiry=False,
            ),
        )
        self._rule(
            listener,
            "NodesForward",
            NODES_FORWARD_PRIORITY,
            [elbv2.ListenerCondition.path_patterns([NODES_PATHS[1]])],
            elbv2.ListenerAction.forward([self.nodes_target_group]),
        )
        self.nodes_listener = listener

    def _nodes_passthrough(self) -> None:
        """Contingencia de R2: balanceador de red con paso directo hasta ``vigia-api``."""
        config = self.config
        self.nodes_target_group = elbv2.NetworkTargetGroup(
            self,
            "ApiNodesTargetGroup",
            target_group_name=target_group_name(config, "tg-api-nodes"),
            vpc=self.foundation.vpc,
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.Protocol.TCP,
            port=PASSTHROUGH_PORT,
            deregistration_delay=DEREGISTRATION_DELAY,
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.TCP,
                interval=HEALTH_INTERVAL,
                healthy_threshold_count=HEALTHY_THRESHOLD,
                unhealthy_threshold_count=UNHEALTHY_THRESHOLD,
            ),
        )
        load_balancer = elbv2.NetworkLoadBalancer(
            self,
            "NodesLoadBalancer",
            load_balancer_name=config.resource_name("nlb-nodes"),
            vpc=self.foundation.vpc,
            vpc_subnets=self._public_subnets(),
            internet_facing=True,
            security_groups=[self.security_groups["sg-alb-nodes"]],
            deletion_protection=not config.ephemeral,
            cross_zone_enabled=True,
        )
        # Un balanceador de red solo escribe registros de escuchas TLS; se activa igualmente
        # para que ningún balanceador quede sin registro (SECURITY-02).
        load_balancer.log_access_logs(self.access_logs_bucket, prefix=NODES_LOG_PREFIX)
        self.nodes_load_balancer = load_balancer
        self.nodes_listener = load_balancer.add_listener(
            "Tcp",
            port=HTTPS_PORT,
            protocol=elbv2.Protocol.TCP,
            default_target_groups=[self.nodes_target_group],
        )

    # --- Cortafuegos (§4.4) ---------------------------------------------------------------

    def _visibility(self, metric: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
        return wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=metric,
            sampled_requests_enabled=True,
        )

    def _rate_rule(
        self,
        name: str,
        priority: int,
        limit: int,
        window: int,
        scope_down: wafv2.CfnWebACL.StatementProperty | None = None,
    ) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            action=wafv2.CfnWebACL.RuleActionProperty(block={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    aggregate_key_type="IP",
                    limit=limit,
                    evaluation_window_sec=window,
                    scope_down_statement=scope_down,
                )
            ),
            visibility_config=self._visibility(name),
        )

    def _managed_rule(
        self, name: str, priority: int, counted: Sequence[str]
    ) -> wafv2.CfnWebACL.RuleProperty:
        overrides = [
            wafv2.CfnWebACL.RuleActionOverrideProperty(
                name=rule, action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={})
            )
            for rule in counted
        ]
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name=name,
                    rule_action_overrides=overrides or None,
                )
            ),
            visibility_config=self._visibility(name),
        )

    def _web_acl(self) -> wafv2.CfnWebACL:
        config = self.config
        name = config.resource_name("app-waf")
        # La ruta se compara decodificada: ``/api%2Fnodes/enrollment`` también cuenta.
        enrollment = wafv2.CfnWebACL.StatementProperty(
            byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
                positional_constraint="EXACTLY",
                search_string=ENROLLMENT_PATH,
                text_transformations=[
                    wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="URL_DECODE")
                ],
            )
        )
        rules = [
            self._rate_rule(
                ENROLLMENT_RATE_RULE,
                0,
                ENROLLMENT_RATE_LIMIT,
                ENROLLMENT_RATE_WINDOW_SECONDS,
                enrollment,
            ),
            self._rate_rule(GENERAL_RATE_RULE, 1, GENERAL_RATE_LIMIT, GENERAL_RATE_WINDOW_SECONDS),
        ]
        rules += [
            self._managed_rule(group, priority, counted)
            for priority, (group, counted) in enumerate(MANAGED_RULE_GROUPS, start=len(rules))
        ]
        acl = wafv2.CfnWebACL(
            self,
            "AppWaf",
            name=name,
            scope=WAF_SCOPE,
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            # El patrón de CloudFormation no admite paréntesis en la descripción.
            description="vigia-app-waf, infrastructure-design 4.4, pendientes 11 y 15",
            rules=rules,
            visibility_config=self._visibility(name),
        )
        wafv2.CfnWebACLAssociation(
            self,
            "AppWafAssociation",
            resource_arn=self.app_load_balancer.load_balancer_arn,
            web_acl_arn=acl.attr_arn,
        )
        log_group = logs.LogGroup(
            self,
            "AppWafLogs",
            log_group_name=waf_log_group_name(config),
            retention=WAF_LOG_RETENTION,
            encryption_key=kms.Key.from_key_arn(
                self, "Key-logs", self.foundation.keys[KeyName.LOGS].key_arn
            ),
            removal_policy=RemovalPolicy.DESTROY if config.ephemeral else RemovalPolicy.RETAIN,
        )
        logging = wafv2.CfnLoggingConfiguration(
            self,
            "AppWafLogging",
            resource_arn=acl.attr_arn,
            # El cortafuegos exige el ARN del grupo sin el ``:*`` final.
            log_destination_configs=[
                self.format_arn(
                    service="logs",
                    resource="log-group",
                    resource_name=waf_log_group_name(config),
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                )
            ],
        )
        logging.node.add_dependency(log_group)
        self._rate_alarm(name)
        return acl

    def _rate_alarm(self, web_acl_name: str) -> cloudwatch.Alarm:
        """Aviso de bloqueos por la regla de tasa general a ``vigia-alerts`` (nº 11)."""
        blocked = cloudwatch.Metric(
            namespace="AWS/WAFV2",
            metric_name="BlockedRequests",
            dimensions_map={
                "WebACL": web_acl_name,
                "Region": self.region,
                "Rule": GENERAL_RATE_RULE,
            },
            statistic="Sum",
            period=RATE_ALARM_PERIOD,
        )
        alarm = cloudwatch.Alarm(
            self,
            "AppWafRateAlarm",
            alarm_name=self.config.resource_name("waf-rate-blocked"),
            alarm_description=(
                "vigia-app-waf bloqueo peticiones por la regla de tasa general "
                "(6 000 por direccion cada 5 min, pendiente 11)"
            ),
            metric=blocked,
            threshold=RATE_ALARM_THRESHOLD,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.foundation.alerts_topic))
        return alarm


__all__ = [
    "ENROLLMENT_PATH",
    "MTLS_HEADERS",
    "EdgeStack",
    "nodes_host",
    "target_group_name",
]
