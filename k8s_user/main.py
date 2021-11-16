import argparse
import base64
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from kubernetes import client, config, utils
import logging
import os
from pprint import pprint
import subprocess
import time
import tempfile
import yaml

DEF_KUBECONFIG = os.getenv("KUBECONFIG", os.path.expanduser("~/.kube/config"))
CLUSTER_USER = "qa-monitor"
KUBECTL_CMD = "kubectl"
EPILOG = f"""Script to generate a monitor (readonly) kubeconfig from an existing
kubeconfig that has full access.  This script will create a new role and
rolebinding.  A default user name '{CLUSTER_USER}' is created and a certificate issued.
WARNING: If a previous rolebinding/cert was issued to this user, it will be overwritten.
Any previous kubeconfigs generated by this script for that user will be no longer valid."""


@dataclass
class ClusterRoleRule:
    groups: list[str]
    resources: list[str]
    verbs: list[str]


# These rules are not advised for hardened security, just a safeguard.  Normally specific resources
# for each api group are specified for seriously hardened security to only those resources that are
# needed. Protect the kubeconfig file generated using these rules.
DEFAULT_RULES = [
    ClusterRoleRule(groups=["*"], resources=["*"], verbs=["list", "get", "watch"])
]
logger = logging.getLogger(os.path.basename(__file__))


class KubeConfig:

    def __init__(self, admin_config_path: str, monitor_user: str = CLUSTER_USER,
                 existing_role: (None, str) = None, context: (None, str) = None):

        self.admin_config_path = admin_config_path
        self.monitor_user = monitor_user
        self.existing_role = existing_role

        config.load_kube_config(config_file=admin_config_path, context=context)
        self.k8s_client = client.ApiClient()

        with open(admin_config_path, "r") as fh:
            self.config_data = yaml.safe_load(fh)

        # Currently, multiple contexts are not supported
        if len(self.config_data['clusters']) > 1:
            raise RuntimeError(f"Config file {admin_config_path} contains multiple contexts")
        self.cluster_index = 0

    @property
    def cluster_name(self) -> str:
        """cluster name is retrieved from source kubeconfig"""
        return self.config_data['clusters'][self.cluster_index]['name']

    @property
    def cluster_server(self) -> str:
        """server is retrieved from source kubeconfig"""
        return self.config_data['clusters'][self.cluster_index]['cluster']['server']

    @property
    def k8s_ca(self) -> str:
        """k8s ca is retrieved from source kubeconfig"""
        return self.config_data['clusters'][self.cluster_index]['cluster']['certificate-authority-data']

    @property
    def role_name(self) -> str:
        """role name is derived from user name unless provided"""
        if self.existing_role is None:
            return f"{self.monitor_user}-role"
        return self.existing_role

    @property
    def role_binding_name(self) -> str:
        """
        role binding name is derived from both user and role which is redundant in some cases
        but helpful in others
        """
        return f"{self.monitor_user}-{self.role_name}"

    @property
    def cert_request_name(self) -> str:
        """cert request is based from user name"""
        return f"{self.monitor_user}-cr"

    @property
    def run_env(self) -> dict:
        """run environement for kubectl commands"""
        run_env = os.environ.copy()
        run_env["KUBECONFIG"] = self.admin_config_path
        return run_env

    def generate_csr(self) -> tuple[bytes, str]:

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=4096,
            backend=default_backend()
        )
        b = x509.CertificateSigningRequestBuilder()
        req = b.subject_name(x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"NC"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, u"RTP"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"NetApp"),
            x509.NameAttribute(NameOID.COMMON_NAME, self.monitor_user)
        ])).sign(private_key, hashes.SHA256(), default_backend())

        cert = req.public_bytes(encoding=serialization.Encoding.PEM)

        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')

        return cert, pem

    def apply_dict_to_k8s(self, resource_data: dict):
        utils.create_from_dict(
            k8s_client=self.k8s_client,
            data=resource_data
        )

    def approve_k8s_csr(self) -> bytes:

        certs_api = client.CertificatesV1beta1Api()

        # Get the CSR
        body = certs_api.read_certificate_signing_request_status(self.cert_request_name)

        # create an approval condition
        approval_condition = client.V1beta1CertificateSigningRequestCondition(
            last_update_time=datetime.now(timezone.utc).astimezone(),
            message='This certificate was approved by Python Client API',
            reason='MyOwnReason',
            type='Approved')

        # patch the existing `body` with the new conditions
        # you might want to append the new conditions to the existing ones
        body.status.conditions = [approval_condition]

        # patch the Kubernetes object
        response = certs_api.replace_certificate_signing_request_approval(
            self.cert_request_name,
            body
        )

        start_timer = datetime.now()
        while response.status.certificate is None:
            time.sleep(5)
            if datetime.now() - start_timer > timedelta(seconds=300):
                raise TimeoutError("Timeout waiting for certificate")
            response = certs_api.read_certificate_signing_request_status(
                name=self.cert_request_name
            )

        signed_cert = base64.b64decode(response.status.certificate)

        return signed_cert

# =========== NO CLI COMMANDS ABOVE THIS LINE ================

    def generate_csr_cli(self) -> tuple[bytes, str]:
        with tempfile.NamedTemporaryFile("wb+") as key_file:
            logging.info(f"Generating cert request {self.cert_request_name}")
            cert_command = [
                "openssl", "req", "-new",
                "-newkey", "rsa:4096",
                "-nodes",
                "-keyout", key_file.name,
                "-subj", f"/CN={self.monitor_user}/O=readers"
            ]
            result = subprocess.run(
                cert_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            if result.returncode != 0:
                raise RuntimeError("Failed to create cert request - " + result.stderr.decode("utf-8"))
            cr = result.stdout

            key_file.seek(0)
            key = key_file.read()

        return cr, key

    def _run_kubectl(self, command_args: list[str]) -> subprocess.CompletedProcess:
        """
        Internal kubectl command method
        :param command_args: args to pass to kubectl
        :return: CompletedProcess object
        """
        cmd = [KUBECTL_CMD]
        cmd.extend(command_args)
        return subprocess.run(
            cmd,
            env=self.run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

    def run_kubectl(self, command_args: list[str]) -> str:
        """
        Run a kubectl command and return stdout
        :param command_args: list of args to pass to kubectl
        :return: str containing stdout
        """
        result = self._run_kubectl(command_args)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to run kubectl args={command_args}"
                               f" - {result.stderr.decode('utf-8')}")
        return result.stdout.decode('utf-8')

    def resource_exists(self, resource_type: str, resource_name: str, namespace: (None, str) = None):
        """
        Check if a k8s resource exists
        :param resource_type: resource type
        :param resource_name: resource name
        :param namespace: namespace (if namespaced resource)
        :return: bool True if resource exists, false otherwise
        """
        command_args = ["get", resource_type, resource_name]
        if namespace is not None:
            command_args.extend(["-n", namespace])
        result = self._run_kubectl(command_args)
        if result.returncode != 0:
            error = result.stderr.decode('utf-8')
            if "NotFound" in error:
                return False
            raise RuntimeError(f"Failed to execute command with args {command_args}")
        return True

    def apply_dict_to_k8s_cli(self, resource_data: dict):
        """
        Apply config to k8s using a dictionary
        :param resource_data: dict containing apiVersion and appropriate schema
        :return:
        """
        with tempfile.NamedTemporaryFile("w") as fp:
            kind = resource_data.get('kind', "unknown")
            logger.info(f"Writing resource data for '{kind}' to temp file {fp.name}")
            yaml.safe_dump(resource_data, fp)
            fp.flush()
            self.run_kubectl(["apply", "-f", fp.name])

    def apply_cluster_role(self, rules: (None, list[ClusterRoleRule]) = None):
        """Create a cluster role for readonly access"""
        if rules is None:
            rules = DEFAULT_RULES
        logger.info(f"Applying cluster role {self.role_name} to k8s")
        cluster_role = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": self.role_name
            },
            "rules": []
        }
        for rule in rules:
            cluster_role["rules"].append({
                "apiGroups": rule.groups,
                "resources": rule.resources,
                "verbs": rule.verbs
            })
        self.apply_dict_to_k8s(cluster_role)
        return self.role_name

    def create_role_binding(self):
        """Creates a cluster role binding for the user"""
        logging.info(f"Creating cluster role binding for user {self.monitor_user} to role {self.role_name}")
        self.run_kubectl([
            "create", "clusterrolebinding", self.role_binding_name,
            "--clusterrole", self.role_name,
            "--user", self.monitor_user
        ])

    def generate_and_approve_cert(self, cert_file_name: str, key_file_name: str):
        """
        Create a certificate for k8s authentication
        :param cert_file_name: Path where the cert file will be saved
        :param key_file_name: Path where the private key will be saved
        :return:
        """

        # Check for existing cert
        if self.resource_exists('csr', self.cert_request_name):
            logger.warning(f"Deleting existing csr '{self.cert_request_name}'")
            self.run_kubectl(["delete", "csr", self.cert_request_name])

        logging.info(f"Generating cert request {self.cert_request_name}")
        cr, pem = self.generate_csr()

        with open(key_file_name, "w") as fh:
            fh.write(pem)

        # Apply the cert request to k8s
        cert_request = {
            "apiVersion": "certificates.k8s.io/v1",
            "kind": "CertificateSigningRequest",
            "metadata": {"name": self.cert_request_name},
            "spec": {
                "signerName": "kubernetes.io/kube-apiserver-client",
                "request": base64.b64encode(cr).decode('utf-8'),
                "usages": ["client auth"]
            }
        }
        self.apply_dict_to_k8s(cert_request)

        # Not sure I really need to do this, but nice to fail here if something failed
        self.verify_cert_request()

        # Approve the certificate
        logging.info(f"Approving cert request {self.cert_request_name}")
        # self.run_kubectl(["certificate", "approve", self.cert_request_name])

        # Get the signed cert and save it to file
        # result = self.run_kubectl(["get", "csr", self.cert_request_name, "-o", "yaml"])
        # cert_data = yaml.safe_load(result)

        # user_cert = cert_data["status"]["certificate"]

        user_cert = self.approve_k8s_csr()

        with open(cert_file_name, "wb") as fp:
            # fp.write(base64.b64decode(user_cert))
            fp.write(user_cert)

    def create_new_kubeconfig(self, cert_file: str, cert_key_file: str, output_file: str):
        """
        Creates the new monitor kubeconfig file
        :param cert_file: path to existing monitor user cert
        :param cert_key_file: path to existing key for user cert
        :param output_file: path to new kubeconfig file to be created
        :return: None
        """

        logger.info(f"Creating new kubeconfig file {output_file}")

        # Save the k8s CA cert from the source config file and create the initial kubeconfig
        with tempfile.NamedTemporaryFile("wb") as ca_file_pointer:
            ca_file_pointer.write(base64.b64decode(self.k8s_ca))
            ca_file_pointer.flush()

            self.run_kubectl([
                "config",
                "set-cluster", self.cluster_name,
                "--server", self.cluster_server,
                "--certificate-authority", ca_file_pointer.name,
                "--kubeconfig", output_file,
                "--embed-certs"
            ])

        # Add the client certificate data and key
        self.run_kubectl([
            "config",
            "set-credentials", self.monitor_user,
            "--client-certificate", cert_file,
            "--client-key", cert_key_file,
            "--kubeconfig", output_file,
            "--embed-certs"
        ])

        # Set the default context
        context_name = f"{self.cluster_name}-{self.monitor_user}"
        self.run_kubectl([
            "config",
            "set-context", context_name,
            "--cluster", self.cluster_name,
            "--namespace", "default",
            "--user", self.monitor_user,
            "--kubeconfig", output_file
        ])

        # Select the default context
        self.run_kubectl([
            "config",
            "use-context", context_name,
            "--kubeconfig", output_file
        ])

    def verify_cert_request(self):
        """
        Verifies a cert request exists
        :return:
        """
        output = self.run_kubectl([
            "get",
            "certificatesigningrequests.certificates.k8s.io",
            "-o", "yaml"
        ])
        certs = yaml.safe_load(output)["items"]
        for cert in certs:
            if cert["metadata"]["name"] == self.cert_request_name:
                return
        raise RuntimeError(f"cert request {self.cert_request_name} not found")

    def generate_monitor_config(self, config_file: str):
        """
        Main logic to generate a monitor (readonly) kubeconfig file
        :param config_file: Path to create the new monitor config file
        :return: None
        """

        # Check if role/binding already exists
        if self.resource_exists('clusterrolebinding', self.role_binding_name):
            ans = input(f"Cluster role binding '{self.role_binding_name}' already exists, overwrite? (y/N)")
            if ans not in ("y", "Y"):
                logger.info("Terminating script by user")
                return
            logger.info(f"Deleting clusterrolebinding '{self.role_binding_name}'")
            self.run_kubectl(["delete", "clusterrolebinding", self.role_binding_name])

        role_exists = self.resource_exists('clusterrole', self.role_name)
        if role_exists and not self.existing_role:
            ans = input(f"Cluster role '{self.role_name}' already exists, overwrite? (y/N)")
            if ans not in ("y", "Y"):
                logger.info("Terminating script by user")
                return
            logger.info(f"Deleting clusterrole '{self.role_name}'")
            self.run_kubectl(["delete", "clusterrole", self.role_name])
        elif self.existing_role and not role_exists:
            raise RuntimeError(f"Role {self.existing_role} specified but does not exist")

        # Create the monitor role/binding in k8s
        if self.existing_role is None:
            self.apply_cluster_role()
        self.create_role_binding()

        # Create a temp directory for the cert files
        with tempfile.TemporaryDirectory() as temp_dir:
            cert_file = os.path.join(temp_dir, "monitor.crt")
            cert_key_file = os.path.join(temp_dir, "monitor.key")

            # Create the cert request and approve it, saving the cert and key files
            self.generate_and_approve_cert(
                cert_file_name=cert_file,
                key_file_name=cert_key_file
            )

            self.create_new_kubeconfig(
                cert_file=cert_file,
                cert_key_file=cert_key_file,
                output_file=config_file
            )


def get_args():
    """Get the command line arguments"""
    parser = argparse.ArgumentParser(description="Create user kubeconfig", epilog=EPILOG)
    parser.add_argument(
        "outfile",
        help="Output file name"
    )
    parser.add_argument(
        "--kubeconfig", "-k",
        default=DEF_KUBECONFIG,
        help=f"Source path to kubeconfig with full access default={DEF_KUBECONFIG}"
    )
    parser.add_argument(
        "--user", "-u",
        default=CLUSTER_USER,
        help=f"K8s user to create/overwrite default={CLUSTER_USER}"
    )
    parser.add_argument(
        "--role", "-r",
        required=False,
        help=f"Existing K8s role to bind to user. Omit to create a role that uses default list/get/watch rules"
    )
    return parser.parse_args()


def main():
    args = get_args()
    logging.basicConfig(level=logging.INFO)
    kube_config = KubeConfig(
        admin_config_path=os.path.expanduser(args.kubeconfig),
        monitor_user=args.user,
        existing_role=args.role
    )
    kube_config.generate_monitor_config(args.outfile)


if __name__ == "__main__":
    main()
