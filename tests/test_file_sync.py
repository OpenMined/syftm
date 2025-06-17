import os
import time
import pytest
from pathlib import Path
from tests.conftest import wait_for_container_log, wait_for_file, get_container_status


@pytest.mark.integration
class TestFileSync:
    """Test file synchronization between SyftBox clients."""
    
    CLIENT1_EMAIL = "client1@syftbox.net"
    CLIENT2_EMAIL = "client2@syftbox.net"
    TEST_FILE_NAME = "test_sync.txt"
    TEST_FILE_CONTENT = "Hello from client1! This is a test file for synchronization."
    
    def test_file_sync_between_clients(self, docker_client, clients_dir):
        """Test that a file written to client1's public folder syncs to client2."""
        
        # Step 1: Verify server is running
        server_status = get_container_status(docker_client, "syftbox-server")
        assert server_status["running"], f"Server is not running. Status: {server_status}"
        print(f"✓ Server is running")
        
        # Step 2: Wait for client1 to connect
        client1_container = "syftbox-client-client1-syftbox-net"
        assert wait_for_container_log(
            client1_container, 
            "socketmgr client connected", 
            timeout=30
        ), f"Client1 failed to connect. Logs: {get_container_status(docker_client, client1_container)['logs']}"
        print(f"✓ Client1 ({self.CLIENT1_EMAIL}) connected")
        
        # Step 3: Wait for client2 to connect
        client2_container = "syftbox-client-client2-syftbox-net"
        assert wait_for_container_log(
            client2_container, 
            "socketmgr client connected", 
            timeout=30
        ), f"Client2 failed to connect. Logs: {get_container_status(docker_client, client2_container)['logs']}"
        print(f"✓ Client2 ({self.CLIENT2_EMAIL}) connected")
        
        # Step 4: Give clients time to initialize their directory structures
        time.sleep(5)
        
        # Debug: Show actual directory structure
        print(f"🔍 Checking directory structure...")
        for client_email in [self.CLIENT1_EMAIL, self.CLIENT2_EMAIL]:
            client_dir = clients_dir / client_email
            if client_dir.exists():
                print(f"  {client_email}: {list(client_dir.iterdir())}")
                syftbox_dir = client_dir / "SyftBox"
                if syftbox_dir.exists():
                    print(f"    SyftBox: {list(syftbox_dir.iterdir())}")
            else:
                print(f"  {client_email}: Directory not found")
        
        # Step 5: Write test file to client1's public folder
        client1_public_dir = clients_dir / self.CLIENT1_EMAIL / "SyftBox" / "datasites" / self.CLIENT1_EMAIL / "public"
        client1_public_dir.mkdir(parents=True, exist_ok=True)
        
        test_file_path = client1_public_dir / self.TEST_FILE_NAME
        test_file_path.write_text(self.TEST_FILE_CONTENT)
        print(f"✓ Test file written to: {test_file_path}")
        
        # Step 6: Wait for file to sync to client2's datasites folder
        # When client1 publishes to their own public folder:
        #   client1@syftbox.net/SyftBox/datasites/client1@syftbox.net/public/file.txt
        # It should sync to client2's view of client1's datasite:
        #   client2@syftbox.net/SyftBox/datasites/client1@syftbox.net/public/file.txt
        expected_sync_path = (
            clients_dir / self.CLIENT2_EMAIL / "SyftBox" / "datasites" / 
            self.CLIENT1_EMAIL / "public" / self.TEST_FILE_NAME
        )
        
        print(f"⏳ Waiting for file to sync to: {expected_sync_path}")
        
        # Check every second for up to 60 seconds (longer wait for cross-client sync)
        synced = False
        for i in range(60):
            if expected_sync_path.exists():
                synced = True
                break
            time.sleep(1)
            if i % 10 == 0:
                print(f"   Still waiting... ({i+1}s)")
                # Also check if the directory structure is being created
                client2_datasites = clients_dir / self.CLIENT2_EMAIL / "SyftBox" / "datasites"
                if client2_datasites.exists():
                    print(f"   Client2 datasites: {list(client2_datasites.iterdir())}")
                else:
                    print(f"   Client2 datasites directory not found")
        
        assert synced, f"File did not sync within 60 seconds. Expected at: {expected_sync_path}"
        print(f"✓ File synced successfully!")
        
        # Step 7: Verify file content
        synced_content = expected_sync_path.read_text()
        assert synced_content == self.TEST_FILE_CONTENT, \
            f"Synced content doesn't match. Expected: '{self.TEST_FILE_CONTENT}', Got: '{synced_content}'"
        print(f"✓ File content verified")
        
        # Step 8: Additional verification - check file permissions and metadata
        assert expected_sync_path.is_file(), "Synced path is not a regular file"
        assert expected_sync_path.stat().st_size > 0, "Synced file is empty"
        print(f"✓ File metadata verified")
        
        print("\n✅ File sync test completed successfully!")


@pytest.mark.integration
class TestMultipleFileSync:
    """Test synchronization of multiple files."""
    
    CLIENT1_EMAIL = "client1@syftbox.net"
    CLIENT2_EMAIL = "client2@syftbox.net"
    
    def test_multiple_files_sync(self, docker_client, clients_dir):
        """Test that multiple files sync correctly."""
        
        # Wait for both clients to be connected
        client1_container = "syftbox-client-client1-syftbox-net"
        client2_container = "syftbox-client-client2-syftbox-net"
        
        assert wait_for_container_log(client1_container, "socketmgr client connected", timeout=30)
        assert wait_for_container_log(client2_container, "socketmgr client connected", timeout=30)
        
        # Give time for initialization
        time.sleep(5)
        
        # Write multiple files
        client1_public_dir = clients_dir / self.CLIENT1_EMAIL / "SyftBox" / "datasites" / self.CLIENT1_EMAIL / "public"
        client1_public_dir.mkdir(parents=True, exist_ok=True)
        
        test_files = {
            "file1.txt": "Content of file 1",
            "file2.txt": "Content of file 2",
            "data.json": '{"test": "data", "number": 42}',
        }
        
        for filename, content in test_files.items():
            file_path = client1_public_dir / filename
            file_path.write_text(content)
            print(f"✓ Created {filename}")
        
        # Wait for all files to sync
        client2_sync_dir = (
            clients_dir / self.CLIENT2_EMAIL / "SyftBox" / "datasites" / 
            self.CLIENT1_EMAIL / "public"
        )
        
        all_synced = True
        for filename, expected_content in test_files.items():
            sync_path = client2_sync_dir / filename
            
            # Wait for file to appear
            if not wait_for_file(sync_path, timeout=30):
                all_synced = False
                print(f"✗ {filename} did not sync")
                continue
            
            # Verify content
            actual_content = sync_path.read_text()
            if actual_content != expected_content:
                all_synced = False
                print(f"✗ {filename} content mismatch")
            else:
                print(f"✓ {filename} synced correctly")
        
        assert all_synced, "Not all files synced correctly"
        print("\n✅ Multiple file sync test completed successfully!")