# Integration test commands for syftbox

# Directory paths
SYFTBOX_DIR := "./syftbox"
TEST_CLIENTS_DIR := "./clients"
DOCKER_DIR := SYFTBOX_DIR + "/docker"

# Test configuration
TEST_CLIENT1_EMAIL := "client1@syftbox.net"
TEST_CLIENT2_EMAIL := "client2@syftbox.net"
TEST_CLIENT1_NAME := "client1-syftbox-net"
TEST_CLIENT2_NAME := "client2-syftbox-net"
TEST_CLIENT1_PORT := "7938"
TEST_CLIENT2_PORT := "7939"

# Default target
default:
    @just --list

# Set up test environment
setup:
    @echo "Setting up test environment..."
    @mkdir -p {{TEST_CLIENTS_DIR}}
    @echo "Test environment ready."

# Start the server with MinIO
start-server:
    @echo "Starting SyftBox server with MinIO..."
    cd {{DOCKER_DIR}} && COMPOSE_BAKE=true docker compose up -d --build minio server
    @echo "Waiting for server to be ready..."
    @sleep 5
    @echo "Server started at http://localhost:8080"

# Start client 1
start-client1:
    @echo "Starting client 1 ({{TEST_CLIENT1_EMAIL}})..."
    @echo "Building client image..."
    @if [ -n "$DOCKER_BUILDX" ]; then \
        cd {{SYFTBOX_DIR}} && docker buildx build --cache-from=type=gha --cache-to=type=gha,mode=max -f docker/Dockerfile.client -t syftbox-client --load .; \
    else \
        cd {{SYFTBOX_DIR}} && docker build -f docker/Dockerfile.client -t syftbox-client .; \
    fi
    @echo "Starting client1 container..."
    docker run -d \
        --name syftbox-client-{{TEST_CLIENT1_NAME}} \
        --network docker_syftbox-network \
        -p {{TEST_CLIENT1_PORT}}:7938 \
        -e SYFTBOX_SERVER_URL=http://syftbox-server:8080 \
        -e SYFTBOX_AUTH_ENABLED=0 \
        -v "$(pwd)/{{TEST_CLIENTS_DIR}}:/data/clients" \
        syftbox-client {{TEST_CLIENT1_EMAIL}}
    @echo "Client 1 started at http://localhost:{{TEST_CLIENT1_PORT}}"

# Start client 2
start-client2:
    @echo "Starting client 2 ({{TEST_CLIENT2_EMAIL}})..."
    @echo "Starting client2 container..."
    docker run -d \
        --name syftbox-client-{{TEST_CLIENT2_NAME}} \
        --network docker_syftbox-network \
        -p {{TEST_CLIENT2_PORT}}:7938 \
        -e SYFTBOX_SERVER_URL=http://syftbox-server:8080 \
        -e SYFTBOX_AUTH_ENABLED=0 \
        -v "$(pwd)/{{TEST_CLIENTS_DIR}}:/data/clients" \
        syftbox-client {{TEST_CLIENT2_EMAIL}}
    @echo "Client 2 started at http://localhost:{{TEST_CLIENT2_PORT}}"

# Start all services
start-all: setup start-server
    @sleep 3
    @just start-client1
    @sleep 2
    @just start-client2
    @echo "All services started!"

# Stop all services
stop-all:
    @echo "Stopping all services..."
    -docker stop syftbox-client-{{TEST_CLIENT1_NAME}}
    -docker stop syftbox-client-{{TEST_CLIENT2_NAME}}
    -cd {{DOCKER_DIR}} && docker compose down
    @echo "All services stopped."

# Quick restart - reset clients and MinIO state without stopping server
quick-restart:
    @echo "Quick restart - resetting clients and storage..."
    # Stop and remove client containers
    -docker stop syftbox-client-{{TEST_CLIENT1_NAME}}
    -docker stop syftbox-client-{{TEST_CLIENT2_NAME}}
    -docker rm syftbox-client-{{TEST_CLIENT1_NAME}}
    -docker rm syftbox-client-{{TEST_CLIENT2_NAME}}
    # Remove client data
    -rm -rf {{TEST_CLIENTS_DIR}}
    # Reset MinIO data by recreating the volume
    -cd {{DOCKER_DIR}} && docker compose stop minio
    -cd {{DOCKER_DIR}} && docker compose rm -f minio
    -docker volume rm docker_minio-data || true
    # Restart MinIO and server
    -cd {{DOCKER_DIR}} && docker compose up -d --build minio server
    @echo "Waiting for server to be ready..."
    @sleep 5
    # Restart clients
    @just start-client1
    @sleep 2
    @just start-client2
    @echo "Quick restart complete!"

# Clean up everything (stop + remove volumes and test data)
clean: stop-all
    @echo "Cleaning up..."
    -docker rm syftbox-client-{{TEST_CLIENT1_NAME}}
    -docker rm syftbox-client-{{TEST_CLIENT2_NAME}}
    -cd {{DOCKER_DIR}} && docker compose down -v
    -rm -rf {{TEST_CLIENTS_DIR}}
    @echo "Cleanup complete."

# Run the integration tests
test:
    @echo "Running integration tests..."
    @echo "Activating virtual environment and running tests..."
    . .venv/bin/activate && python -m pytest tests/ -v

# Install test dependencies
install-deps:
    @echo "Installing test dependencies with uv..."
    uv venv --python 3.11
    uv pip install -r requirements-test.txt

# Run tests with setup and teardown
test-full: clean start-all
    @echo "Waiting for services to stabilize..."
    @sleep 10
    @echo "Running tests..."
    -. .venv/bin/activate && python -m pytest tests/ -v
    @just clean

# Show logs for debugging
logs-server:
    docker logs syftbox-server -f

logs-client1:
    docker logs syftbox-client-{{TEST_CLIENT1_NAME}} -f

logs-client2:
    docker logs syftbox-client-{{TEST_CLIENT2_NAME}} -f

# Check service status
status:
    @echo "Service status:"
    @docker ps --filter "name=syftbox"