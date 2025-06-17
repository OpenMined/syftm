import os
import time
import docker
import pytest
import subprocess
from pathlib import Path
from typing import Generator, Dict, Any


@pytest.fixture(scope="session")
def docker_client() -> docker.DockerClient:
    """Create a Docker client instance."""
    return docker.from_env()


@pytest.fixture(scope="session")
def test_root_dir() -> Path:
    """Get the test root directory."""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def clients_dir(test_root_dir: Path) -> Path:
    """Get the clients directory path."""
    return test_root_dir / "clients"


def wait_for_container_log(container_name: str, expected_text: str, timeout: int = 60) -> bool:
    """Wait for specific text to appear in container logs."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["docker", "logs", container_name],
                capture_output=True,
                text=True,
                check=False
            )
            if expected_text in result.stdout or expected_text in result.stderr:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def wait_for_file(file_path: Path, timeout: int = 30) -> bool:
    """Wait for a file to exist."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if file_path.exists():
            return True
        time.sleep(1)
    return False


def get_container_status(docker_client: docker.DockerClient, container_name: str) -> Dict[str, Any]:
    """Get container status information."""
    try:
        container = docker_client.containers.get(container_name)
        return {
            "status": container.status,
            "running": container.status == "running",
            "logs": container.logs(tail=50).decode("utf-8")
        }
    except docker.errors.NotFound:
        return {"status": "not_found", "running": False, "logs": ""}


@pytest.fixture
def cleanup_containers(docker_client: docker.DockerClient) -> Generator[None, None, None]:
    """Cleanup containers after test."""
    yield
    # Cleanup will be handled by the justfile commands
    # This fixture is here for potential custom cleanup needs