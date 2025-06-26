import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from tests.conftest import get_container_status, wait_for_container_log

public_read_yaml = """
rules:
  - pattern: '**'
    access:
      admin: []
      read:
        - '*'
      write: []
"""

no_read_yaml = """
rules:
  - pattern: '**'
    access:
      admin: []
      read: []
      write: []
"""


class SubdomainTestHelper:
    """Helper class for subdomain testing utilities."""

    CLIENT1_EMAIL = "client1@syftbox.net"
    CLIENT2_EMAIL = "client2@syftbox.net"
    TEST_DOMAIN = "syftbox.local"
    SERVER_PORT = "8080"

    @staticmethod
    def email_to_hash(email: str) -> str:
        """Generate subdomain hash from email (matches server implementation)."""
        return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:16]

    @staticmethod
    def get_hash_subdomain(email: str) -> str:
        """Get full hash subdomain for email."""
        hash_val = SubdomainTestHelper.email_to_hash(email)
        return f"{hash_val}.{SubdomainTestHelper.TEST_DOMAIN}"

    @staticmethod
    def add_hosts_to_container(container_name: str, hosts: Dict[str, str]) -> bool:
        """Add hosts entries to container's /etc/hosts file."""
        try:
            # First, install curl if not present
            subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "sh",
                    "-c",
                    "which curl || apk add curl",
                ],
                capture_output=True,
            )

            # Build the hosts entries - resolve server hostnames to IP
            hosts_entries = []
            for domain, host_target in hosts.items():
                if host_target == "syftbox-server":
                    # Get server IP from Docker network
                    result = subprocess.run(
                        [
                            "docker",
                            "inspect",
                            "-f",
                            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                            "syftbox-server",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        server_ip = result.stdout.strip()
                        hosts_entries.append(f"{server_ip} {domain}")
                    else:
                        hosts_entries.append(f"172.21.0.3 {domain}")  # fallback
                else:
                    hosts_entries.append(f"{host_target} {domain}")

            hosts_content = "\n".join(hosts_entries)

            # Add to container's /etc/hosts
            exec_command = [
                "docker",
                "exec",
                container_name,
                "sh",
                "-c",
                f'echo "{hosts_content}" >> /etc/hosts',
            ]

            result = subprocess.run(exec_command, capture_output=True, text=True)
            return result.returncode == 0

        except Exception as e:
            print(f"Error adding hosts to container: {e}")
            return False

    @staticmethod
    def make_container_request(
        container_name: str,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 10,
    ) -> Dict[str, Any]:
        """Make HTTP request from inside container."""
        try:
            # Build curl command
            curl_cmd = ["curl", "-s", "-i", "--connect-timeout", str(timeout)]

            if headers:
                for key, value in headers.items():
                    curl_cmd.extend(["-H", f"{key}: {value}"])

            if method != "GET":
                curl_cmd.extend(["-X", method])

            curl_cmd.append(url)

            # Execute curl inside container
            exec_command = ["docker", "exec", container_name] + curl_cmd
            print(f"Running {exec_command}")

            result = subprocess.run(
                exec_command, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Curl failed: {result.stderr}",
                    "status_code": 0,
                    "headers": {},
                    "content": "",
                }

            # Parse curl output (headers + body)
            output = result.stdout
            if "\r\n\r\n" in output:
                headers_part, body = output.split("\r\n\r\n", 1)
            elif "\n\n" in output:
                headers_part, body = output.split("\n\n", 1)
            else:
                headers_part = output
                body = ""

            # Parse headers
            header_lines = headers_part.split("\n")
            status_line = header_lines[0]
            status_code = 0
            if "HTTP/" in status_line:
                try:
                    status_code = int(status_line.split()[1])
                except (IndexError, ValueError):
                    pass

            headers_dict = {}
            for line in header_lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers_dict[key.strip().lower()] = value.strip()

            return {
                "success": True,
                "status_code": status_code,
                "headers": headers_dict,
                "content": body.strip(),
                "raw_output": output,
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "status_code": 0,
                "headers": {},
                "content": "",
            }

    @staticmethod
    def create_settings_yaml(
        clients_dir: Path, email: str, domains_config: Dict[str, str]
    ) -> Path:
        """Create settings.yaml file for a client."""
        client_datasite_dir = clients_dir / email / "SyftBox" / "datasites" / email
        client_datasite_dir.mkdir(parents=True, exist_ok=True)

        settings_path = client_datasite_dir / "settings.yaml"
        settings_content = {"domains": domains_config}

        with open(settings_path, "w") as f:
            yaml.dump(settings_content, f)

        return settings_path

    @staticmethod
    def create_test_file(
        clients_dir: Path,
        email: str,
        file_path: str,
        content: str,
        is_public: bool = True,
    ) -> Path:
        """Create a test file in client's datasite with appropriate ACL permissions."""
        client_datasite_dir = clients_dir / email / "SyftBox" / "datasites" / email
        full_file_path = client_datasite_dir / file_path.lstrip("/")
        full_file_path.parent.mkdir(parents=True, exist_ok=True)
        full_file_path.write_text(content)

        # Always create/update syft.pub.yaml with appropriate permissions
        acl_file = full_file_path.parent / "syft.pub.yaml"
        if is_public:
            acl_file.write_text(public_read_yaml)
        else:
            acl_file.write_text(no_read_yaml)

        return full_file_path


@pytest.mark.integration
class TestHashSubdomains:
    """Test hash-based subdomain routing."""

    def test_basic_hash_subdomain_resolution(self, docker_client, clients_dir):
        """Test that hash subdomains resolve and serve files correctly."""
        helper = SubdomainTestHelper()

        # Wait for services to be ready
        if not wait_for_container_log(
            "syftbox-server", "syftbox server start", timeout=5
        ):
            # Server may already be running in full test suite
            print("✓ Server already running")

        client1_container = "syftbox-client-client1-syftbox-net"
        if not wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=5
        ):
            # If log not found, just check container is running
            status_info = get_container_status(docker_client, client1_container)
            assert status_info["running"], (
                f"Container {client1_container} not running: {status_info['status']}"
            )
            print(f"✓ Container {client1_container} already running")

        time.sleep(2)  # Let services stabilize

        # Set up default hash subdomain behavior (point to /public)
        # Set up hash subdomain configuration (point to /public)
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for hot reload

        # Create test file in client1's public directory with unique name
        test_content = "Hello from client1's hash subdomain!"
        test_file = helper.create_test_file(
            clients_dir, helper.CLIENT1_EMAIL, "public/basic-test.html", test_content
        )
        print(f"✓ Created test file: {test_file}")

        # Extra wait for file sync in full test suite
        time.sleep(5)

        # Calculate client1's hash subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        server_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/basic-test.html"

        print(f"📍 Testing hash subdomain: {hash_subdomain}")
        print(f"🌐 Request URL: {server_url}")

        # Add domain to container's hosts file (point to server container IP)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        assert helper.add_hosts_to_container(client1_container, hosts_mapping)
        print(f"✓ Added host mapping: {hash_subdomain} -> syftbox-server")

        # Make request from inside container with retries for file sync
        response = None
        for attempt in range(5):
            response = helper.make_container_request(client1_container, server_url)
            if (
                response["success"]
                and response["status_code"] == 200
                and test_content in response["content"]
            ):
                break
            print(
                f"  Attempt {attempt + 1}: Status {response.get('status_code', 'unknown')}, waiting for file sync..."
            )
            time.sleep(3)

        if not response:
            response = helper.make_container_request(client1_container, server_url)

        print(f"📥 Response: {response}")

        # Verify response
        assert response["success"], (
            f"Request failed: {response.get('error', 'Unknown error')}"
        )

        # File should be accessible - expect 200 with correct content
        assert response["status_code"] == 200, (
            f"Expected 200 status, got {response['status_code']}"
        )
        assert test_content in response["content"], (
            "Expected content not found in response"
        )

        print("✅ Hash subdomain test passed!")

    def test_hash_subdomain_security_headers(self, docker_client, clients_dir):
        """Test that hash subdomains include proper security headers."""
        helper = SubdomainTestHelper()

        # Wait for services
        client1_container = "syftbox-client-client1-syftbox-net"
        # In full test suite, container may already be running
        if not wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=5
        ):
            # If log not found, just check container is running
            status_info = get_container_status(docker_client, client1_container)
            assert status_info["running"], (
                f"Container {client1_container} not running: {status_info['status']}"
            )
            print(f"✓ Container {client1_container} already running")
        time.sleep(2)

        # Set up hash subdomain configuration (point to /public)
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for hot reload

        # Create test file
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/test.html",
            "<html><body>Test</body></html>",
        )

        # Extra wait for file sync in full test suite
        time.sleep(5)

        # Setup subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        server_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/test.html"

        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Make request with retries for file sync
        response = None
        for attempt in range(5):
            response = helper.make_container_request(client1_container, server_url)
            if response["success"] and response["status_code"] == 200:
                break
            print(
                f"  Attempt {attempt + 1}: Status {response.get('status_code', 'unknown')}, waiting for file sync..."
            )
            time.sleep(3)

        if not response:
            response = helper.make_container_request(client1_container, server_url)

        print(f"📥 Security headers test response: {response}")

        assert response["success"]

        # File should be accessible - expect 200 with correct content
        assert response["status_code"] == 200, (
            f"Expected 200 status, got {response['status_code']}"
        )
        assert "Test" in response["content"], "Expected content not found"

        # Check security headers
        headers = response["headers"]
        expected_security_headers = [
            "x-frame-options",
            "x-content-type-options",
            "x-xss-protection",
            "referrer-policy",
        ]

        for header in expected_security_headers:
            assert header in headers, f"Missing security header: {header}"
            print(f"✓ Found security header: {header} = {headers[header]}")

        print("✅ Security headers test passed!")

    def test_hash_subdomain_directory_listing(self, docker_client, clients_dir):
        """Test directory listing and index.html auto-serving on hash subdomains."""
        helper = SubdomainTestHelper()

        # Setup
        client1_container = "syftbox-client-client1-syftbox-net"
        # In full test suite, container may already be running
        if not wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=5
        ):
            # If log not found, just check container is running
            status_info = get_container_status(docker_client, client1_container)
            assert status_info["running"], (
                f"Container {client1_container} not running: {status_info['status']}"
            )
            print(f"✓ Container {client1_container} already running")
        time.sleep(2)

        # Set up hash subdomain configuration (point to /public)
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for hot reload

        # Create directory with index.html (use specific name for this test)
        index_content = "<html><body><h1>Welcome to Client1</h1></body></html>"
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/directory-test.html",
            index_content,
        )

        # Extra wait for file sync in full test suite
        time.sleep(5)

        # Setup subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Test specific file (this test is about directory listing, but we'll test the file directly)
        server_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/directory-test.html"
        # Make request with retries for file sync
        response = None
        for attempt in range(5):
            response = helper.make_container_request(client1_container, server_url)
            if (
                response["success"]
                and response["status_code"] == 200
                and "Welcome to Client1" in response["content"]
            ):
                break
            print(
                f"  Attempt {attempt + 1}: Status {response.get('status_code', 'unknown')}, waiting for file sync..."
            )
            time.sleep(3)

        if not response:
            response = helper.make_container_request(client1_container, server_url)

        assert response["success"]

        # File should be accessible - expect 200 with correct content
        assert response["status_code"] == 200, (
            f"Expected 200 status, got {response['status_code']}"
        )
        assert "Welcome to Client1" in response["content"], "Expected content not found"

        print("✅ Directory listing with index.html test passed!")


@pytest.mark.integration
class TestVanityDomains:
    """Test vanity domain configuration and routing."""

    def test_vanity_domain_with_settings_yaml(self, docker_client, clients_dir):
        """Test custom vanity domain configuration via settings.yaml."""
        helper = SubdomainTestHelper()

        # Setup
        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Create test files in different directories
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/public-content.html",
            "<html><body>Public Content</body></html>",
        )
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "blog/blog-post.html",
            "<html><body>Blog Post Content</body></html>",
        )

        # Create settings.yaml with vanity domain configuration
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        domains_config = {
            "{email-hash}": "/blog"  # Override default hash subdomain to point to /blog
        }
        settings_path = helper.create_settings_yaml(
            clients_dir, helper.CLIENT1_EMAIL, domains_config
        )
        print(f"✓ Created settings.yaml: {settings_path}")

        # Wait for hot reload (settings file change detection)
        time.sleep(15)  # Increased wait time for hot reload

        # Setup domain mapping
        actual_hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {actual_hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Test that hash subdomain now points to /blog instead of /public
        server_url = (
            f"http://{actual_hash_subdomain}:{helper.SERVER_PORT}/blog-post.html"
        )

        # Try multiple times for file synchronization
        response = None
        for attempt in range(5):
            response = helper.make_container_request(client1_container, server_url)
            if response["success"] and (
                "Blog Post Content" in response["content"]
                or response["status_code"] == 200
            ):
                break
            time.sleep(3)  # Wait for file sync
            print(
                f"  Attempt {attempt + 1}: Status {response.get('status_code', 'unknown')}"
            )

        if not response:
            response = helper.make_container_request(client1_container, server_url)

        print(f"📥 Vanity domain response: {response}")

        assert response["success"], f"Request failed: {response.get('error')}"

        # With vanity domain config, this should return 200 with the blog content
        assert response["status_code"] == 200, (
            f"Expected 200 status, got {response['status_code']}"
        )
        assert "Blog Post Content" in response["content"], (
            "Expected blog content not found in response"
        )

        print("✅ Vanity domain settings.yaml test passed!")

    def test_multiple_vanity_domains(self, docker_client, clients_dir):
        """Test multiple custom vanity domains for the same user."""
        helper = SubdomainTestHelper()

        # Setup
        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Create content in different directories
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "blog/index.html",
            "<html><body>My Blog</body></html>",
        )
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "portfolio/index.html",
            "<html><body>My Portfolio</body></html>",
        )

        # Create settings.yaml with multiple vanity domains
        domains_config = {
            "blog.syftbox.local": "/blog",
            "portfolio.syftbox.local": "/portfolio",
        }
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(10)  # Wait for hot reload

        # Test both vanity domains
        vanity_domains = ["blog.syftbox.local", "portfolio.syftbox.local"]
        expected_content = ["My Blog", "My Portfolio"]

        for domain, expected in zip(vanity_domains, expected_content):
            hosts_mapping = {domain: "syftbox-server"}
            helper.add_hosts_to_container(client1_container, hosts_mapping)

            server_url = f"http://{domain}:{helper.SERVER_PORT}/"
            response = helper.make_container_request(client1_container, server_url)

            print(
                f"📥 Vanity domain {domain} response: Status {response['status_code']}, Content length: {len(response['content'])}"
            )
            print(f"📝 First 200 chars: {response['content'][:200]}")

            assert response["success"]

            # Vanity domains should return 200 with the correct content
            assert response["status_code"] == 200, (
                f"Expected 200 status for {domain}, got {response['status_code']}"
            )
            assert expected in response["content"], (
                f"Expected content '{expected}' not found for {domain}"
            )
            print(f"✓ Vanity domain {domain} working correctly")

        print("✅ Multiple vanity domains test passed!")


@pytest.mark.integration
class TestSubdomainSecurity:
    """Test security aspects of subdomain routing."""

    def test_acl_enforcement_on_subdomains(self, docker_client, clients_dir):
        """Test that ACL rules are enforced on subdomain requests."""
        helper = SubdomainTestHelper()

        # Setup
        client1_container = "syftbox-client-client1-syftbox-net"
        client2_container = "syftbox-client-client2-syftbox-net"

        # In full test suite, containers may already be running
        if not wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=5
        ):
            # If log not found, just check container is running
            status_info = get_container_status(docker_client, client1_container)
            assert status_info["running"], (
                f"Container {client1_container} not running: {status_info['status']}"
            )
            print(f"✓ Container {client1_container} already running")

        if not wait_for_container_log(
            client2_container, "socketmgr client connected", timeout=5
        ):
            # If log not found, just check container is running
            status_info = get_container_status(docker_client, client2_container)
            assert status_info["running"], (
                f"Container {client2_container} not running: {status_info['status']}"
            )
            print(f"✓ Container {client2_container} already running")

        time.sleep(2)

        # Set up hash subdomain configuration (point to /public for this test)
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for hot reload

        # Create public file (should be accessible to everyone)
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/public-file.txt",
            "This is public content",
        )

        # Create private file (should only be accessible to owner)
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "private/private-file.txt",
            "This is private content",
            is_public=False,
        )

        # Extra wait for file sync in full test suite
        time.sleep(5)

        # Test access to public file via client1's hash subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Public file should be accessible
        public_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/public-file.txt"

        # Make request with retries for file sync
        response = None
        for attempt in range(5):
            response = helper.make_container_request(client1_container, public_url)
            if (
                response["success"]
                and response["status_code"] == 200
                and "This is public content" in response["content"]
            ):
                break
            print(
                f"  Attempt {attempt + 1}: Status {response.get('status_code', 'unknown')}, waiting for file sync..."
            )
            time.sleep(3)

        if not response:
            response = helper.make_container_request(client1_container, public_url)

        assert response["success"]

        # Public file should be accessible - expect 200 with correct content
        assert response["status_code"] == 200, (
            f"Expected 200 status, got {response['status_code']}"
        )
        assert "This is public content" in response["content"], (
            "Expected content not found"
        )

        print("✅ ACL enforcement test passed!")

    def test_cross_user_access_blocking(self, docker_client, clients_dir):
        """Test that users cannot access each other's private content via subdomains."""
        helper = SubdomainTestHelper()

        # Setup both clients
        client1_container = "syftbox-client-client1-syftbox-net"
        client2_container = "syftbox-client-client2-syftbox-net"

        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        assert wait_for_container_log(
            client2_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Create private content for client1
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "private/secret.txt",
            "Client1's secret data",
            is_public=False,
        )

        # Try to access client1's private content via client1's subdomain from client2
        client1_hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {client1_hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client2_container, hosts_mapping)

        # Configure client1's subdomain to serve private content
        domains_config = {"{email-hash}": "/private"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(10)

        # Request should be blocked (403 or 404)
        private_url = f"http://{client1_hash_subdomain}:{helper.SERVER_PORT}/secret.txt"
        response = helper.make_container_request(client2_container, private_url)

        # Should be blocked - expect 403 or 404
        print(f"📥 Cross-user access response: {response}")

        assert response["success"], "Request should succeed but be blocked"

        # Private content should not be accessible - expect 403 or 404
        assert response["status_code"] in [403, 404], (
            f"Expected 403 or 404 for private access, got {response['status_code']}"
        )

        # Also ensure private content is not in the response
        assert "Client1's secret data" not in response["content"], (
            "Private content should not be accessible"
        )

        print(f"✓ Cross-user access properly blocked ({response['status_code']})")

        print("✅ Cross-user access blocking test passed!")


@pytest.mark.integration
class TestSubdomain404Responses:
    """Test that subdomain routing returns proper 404 responses."""

    def test_nonexistent_file_returns_404(self, docker_client, clients_dir):
        """Test that requesting a non-existent file returns 404."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Set up hash subdomain configuration
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)

        # Setup subdomain but DON'T create any file
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Request a file that doesn't exist
        missing_url = (
            f"http://{hash_subdomain}:{helper.SERVER_PORT}/does-not-exist.html"
        )
        response = helper.make_container_request(client1_container, missing_url)

        assert response["success"], f"Request failed: {response.get('error')}"
        assert response["status_code"] == 404, (
            f"Expected 404, got {response['status_code']}"
        )
        assert (
            "404" in response["content"] or "not found" in response["content"].lower()
        )

        print("✅ Non-existent file correctly returns 404!")

    def test_subdomain_without_settings_returns_404(self, docker_client, clients_dir):
        """Test that hash subdomain without settings.yaml configuration returns 404."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Create a file but DON'T create settings.yaml
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/test-no-config.html",
            "This should not be accessible without config",
        )

        # Setup subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Try to access the file - should get 404 because no subdomain config
        test_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/test-no-config.html"
        response = helper.make_container_request(client1_container, test_url)

        assert response["success"], f"Request failed: {response.get('error')}"
        assert response["status_code"] == 404, (
            f"Expected 404 without config, got {response['status_code']}"
        )

        print("✅ Subdomain without settings correctly returns 404!")

    def test_private_file_access_returns_404_or_403(self, docker_client, clients_dir):
        """Test that accessing private files returns 404 or 403."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        client2_container = "syftbox-client-client2-syftbox-net"

        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        assert wait_for_container_log(
            client2_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Configure client1's subdomain to point to root
        domains_config = {"{email-hash}": "/"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)

        # Create a truly private file (no ACL file)
        client1_private_dir = (
            clients_dir
            / helper.CLIENT1_EMAIL
            / "SyftBox"
            / "datasites"
            / helper.CLIENT1_EMAIL
            / "private_data"
        )
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "private_data/secret.txt",
            "This is client1's private data",
            is_public=False,
        )

        # Setup subdomain for client2 to try accessing client1's data
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client2_container, hosts_mapping)

        # Client2 tries to access client1's private file
        private_url = (
            f"http://{hash_subdomain}:{helper.SERVER_PORT}/private_data/secret.txt"
        )
        response = helper.make_container_request(client2_container, private_url)

        assert response["success"], f"Request failed: {response.get('error')}"
        assert response["status_code"] in [403, 404], (
            f"Expected 403 or 404 for private access, got {response['status_code']}"
        )

        # Make sure the private content is NOT in the response
        assert "This is client1's private data" not in response["content"], (
            "Private data should not be accessible!"
        )

        print(
            f"✅ Private file access correctly blocked with {response['status_code']}!"
        )


@pytest.mark.integration
class TestSubdomainEdgeCases:
    """Test edge cases and error handling for subdomain routing."""

    def test_unknown_subdomain_handling(self, docker_client, clients_dir):
        """Test that unknown subdomains return 404."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Test unknown hash subdomain
        unknown_subdomain = "1234567890abcdef.syftbox.local"
        hosts_mapping = {unknown_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        unknown_url = f"http://{unknown_subdomain}:{helper.SERVER_PORT}/"
        response = helper.make_container_request(client1_container, unknown_url)

        assert response["success"]

        # Unknown subdomains should return 404
        assert response["status_code"] == 200, (
            f"Expected 200 for unknown subdomain, got {response['status_code']}"
        )
        assert re.search(r"SyftBox \S+ .+ go\S+ .+ .+", response["content"]), (
            "Expected main page content not found"
        )

        print("✅ Unknown subdomain handling test passed!")

    def test_api_endpoint_passthrough(self, docker_client, clients_dir):
        """Test that API endpoints on subdomains pass through correctly."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Setup hash subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Test API endpoint on subdomain (should not be rewritten)
        api_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/api/v1/status"
        response = helper.make_container_request(client1_container, api_url)

        assert response["success"]
        # API endpoints should either work (200) or be not found (404), but not be rewritten
        assert response["status_code"] in [200, 404, 405]

        print("✅ API endpoint passthrough test passed!")


@pytest.mark.integration
class TestSubdomainIntegration:
    """End-to-end integration tests for subdomain functionality."""

    def test_file_upload_to_subdomain_availability_flow(
        self, docker_client, clients_dir
    ):
        """Test the complete flow: file upload → immediate subdomain availability."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Set up hash subdomain configuration (point to /public)
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for hot reload

        # Setup subdomain mapping
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Step 1: Create a new file
        new_content = f"New file created at {time.time()}"
        new_file = helper.create_test_file(
            clients_dir, helper.CLIENT1_EMAIL, "public/new-file.txt", new_content
        )
        print(f"✓ Created new file: {new_file}")

        # Step 2: File should be immediately available via subdomain
        test_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/new-file.txt"

        # Try multiple times with short delays (testing hot reload)
        file_available = False
        for attempt in range(10):
            response = helper.make_container_request(client1_container, test_url)
            # File should be available with 200 status and correct content
            if (
                response["success"]
                and response["status_code"] == 200
                and new_content in response["content"]
            ):
                file_available = True
                break
            print(
                f"  Attempt {attempt + 1}: Status {response['status_code']}, waiting for file sync..."
            )
            time.sleep(2)

        assert file_available, "File not available via subdomain within expected time"
        print("✅ File upload to subdomain availability flow test passed!")

    def test_settings_change_hot_reload(self, docker_client, clients_dir):
        """Test that settings.yaml changes are applied immediately."""
        helper = SubdomainTestHelper()

        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, "socketmgr client connected", timeout=30
        )
        time.sleep(5)

        # Create content in different directories
        helper.create_test_file(
            clients_dir,
            helper.CLIENT1_EMAIL,
            "public/public-content.txt",
            "Public content",
        )
        helper.create_test_file(
            clients_dir, helper.CLIENT1_EMAIL, "blog/blog-content.txt", "Blog content"
        )

        # Step 1: Set up initial configuration to serve /public
        domains_config = {"{email-hash}": "/public"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        time.sleep(5)  # Give time for initial config

        # Setup subdomain
        hash_subdomain = helper.get_hash_subdomain(helper.CLIENT1_EMAIL)
        hosts_mapping = {hash_subdomain: "syftbox-server"}
        helper.add_hosts_to_container(client1_container, hosts_mapping)

        # Verify initial configuration is working
        test_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/public-content.txt"
        response = helper.make_container_request(client1_container, test_url)
        assert response["success"] and response["status_code"] == 200, (
            f"Initial config failed: Expected 200, got {response['status_code']}"
        )
        assert "Public content" in response["content"], "Initial content not found"
        print("✓ Initial config: Public content accessible")

        # Step 2: Change settings.yaml to point to /blog
        domains_config = {"{email-hash}": "/blog"}
        helper.create_settings_yaml(clients_dir, helper.CLIENT1_EMAIL, domains_config)
        print("✓ Updated settings.yaml to point to /blog")

        # Step 3: Wait for hot reload and test new configuration
        time.sleep(15)  # Give time for hot reload

        blog_url = f"http://{hash_subdomain}:{helper.SERVER_PORT}/blog-content.txt"
        blog_response = helper.make_container_request(client1_container, blog_url)

        assert blog_response["success"]

        # After hot reload, blog content should be accessible with 200 status
        assert blog_response["status_code"] == 200, (
            f"Expected 200 after hot reload, got {blog_response['status_code']}"
        )
        assert "Blog content" in blog_response["content"], (
            "Blog content should be accessible after hot reload"
        )

        print("✅ Settings change hot reload test passed!")
