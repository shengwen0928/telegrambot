import unittest
import os
import json
from src.persistence import save_tasks_to_file, load_tasks_from_file
from src.monitor import HohsinMonitor
from src.tr_monitor import TaiwanRailwayMonitor

class MockNotifier:
    async def send_message(self, text):
        pass

class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tasks_file = "tasks.json"
        if os.path.exists(self.tasks_file):
            os.remove(self.tasks_file)
        self.notifier = MockNotifier()

    def tearDown(self):
        if os.path.exists(self.tasks_file):
            os.remove(self.tasks_file)

    def test_save_and_load_hohsin(self):
        monitor = HohsinMonitor(
            from_station="1",
            to_station="2",
            travel_date="2026-05-10",
            user_phone="0912345678",
            user_password="password",
            notifier=self.notifier
        )
        running_tasks = {"user1": [monitor]}
        save_tasks_to_file(running_tasks)
        
        self.assertTrue(os.path.exists(self.tasks_file))
        
        loaded = load_tasks_from_file()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["user_id"], "user1")
        self.assertEqual(loaded[0]["bus_type"], "hohsin")
        self.assertEqual(loaded[0]["params"]["from_station"], "1")
        self.assertEqual(loaded[0]["params"]["user_phone"], "0912345678")

    def test_save_and_load_tra(self):
        monitor = TaiwanRailwayMonitor(
            from_station="1000",
            to_station="3300",
            travel_date="2026-05-10",
            start_time="08:00",
            end_time="12:00",
            notifier=self.notifier,
            user_id_no="A123456789",
            user_password="tra_password"
        )
        running_tasks = {"user2": [monitor]}
        save_tasks_to_file(running_tasks)
        
        loaded = load_tasks_from_file()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["user_id"], "user2")
        self.assertEqual(loaded[0]["bus_type"], "tra")
        self.assertEqual(loaded[0]["params"]["user_id_no"], "A123456789")

if __name__ == "__main__":
    unittest.main()
