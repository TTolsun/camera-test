import unittest

from camera_runner.adb import camera_client_facing


class ActiveCameraFacingTests(unittest.TestCase):
    package = "com.sec.android.app.camera"
    metadata = """
== Camera HAL device device@1.1/internal/20 (v1.3) static information: ==
    Facing: Back
== Camera HAL device device@1.1/internal/20 (v1.3) dumpState: ==
== Camera HAL device device@1.1/internal/1 (v1.3) static information: ==
    Facing: Front
"""

    def client(self, camera, package):
        return f"(Camera ID: {camera}, Cost: 66, Client Package Name: {package}, Conflicting Client Devices: {{}})\n"

    def test_joins_active_client_and_static_metadata_by_id_and_exact_package(self):
        clients = self.client("20", self.package + ".other") + self.client("1", self.package)
        self.assertEqual(camera_client_facing(clients + self.metadata, self.package), "front")
        self.assertEqual(camera_client_facing(self.client("20", self.package) + self.metadata, self.package), "rear")

    def test_missing_external_or_mixed_metadata_never_proves_direction(self):
        for clients in ("", self.client("8", self.package), self.client("8", self.package) + self.client("1", self.package),
                        self.client("1", self.package) + self.client("20", self.package)):
            with self.subTest(clients=clients):
                self.assertIsNone(camera_client_facing(clients + self.metadata, self.package))
