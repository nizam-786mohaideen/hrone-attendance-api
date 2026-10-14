import unittest
from datetime import datetime, timezone

from app.attendance import attendance_metrics
from app.main import attendance_output


EMPLOYEE = {"shift_start": "09:30", "shift_end": "18:30"}


def utc(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class AttendanceMetricsTests(unittest.TestCase):
    def test_normal_attendance(self):
        result = attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:00:00"), utc("2026-10-14T13:00:00"))
        self.assertEqual(result, {"work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})

    def test_late_arrival_uses_shift_start_and_grace(self):
        result = attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:45:00"), None)
        self.assertEqual(result["late_minutes"], 45)
        self.assertIsNone(result["work_hours"])

    def test_early_departure_does_not_create_overtime(self):
        result = attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:00:00"), utc("2026-10-14T12:00:00"))
        self.assertEqual(result["work_hours"], 8.0)
        self.assertEqual(result["overtime_minutes"], 0)

    def test_overtime_uses_shift_end(self):
        result = attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:00:00"), utc("2026-10-14T14:00:00"))
        self.assertEqual(result["overtime_minutes"], 60)

    def test_missing_punches(self):
        self.assertEqual(attendance_metrics("2026-10-14", EMPLOYEE, None, None),
                         {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})

    def test_invalid_order_is_rejected(self):
        with self.assertRaises(ValueError):
            attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:00:00"), utc("2026-10-14T03:00:00"))

    def test_half_day_threshold_is_four_and_half_hours(self):
        result = attendance_metrics("2026-10-14", EMPLOYEE, utc("2026-10-14T04:00:00"), utc("2026-10-14T08:29:00"))
        self.assertTrue(result["half_day"])

    def test_overnight_shift_bounds(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        result = attendance_metrics("2026-10-14", employee, utc("2026-10-14T16:30:00"), utc("2026-10-15T01:00:00"))
        self.assertEqual(result["work_hours"], 8.5)
        self.assertEqual(result["late_minutes"], 0)
        self.assertEqual(result["overtime_minutes"], 30)

    def test_history_output_normalizes_legacy_fields(self):
        result = attendance_output({
            "emp_code": "EMP1234", "date": "2026-10-14", "status": "PRESENT",
            "history": [
                {"changed_at": utc("2026-10-14T04:00:00"), "changed_fields": {"status": {"old": "ABSENT", "new": "PRESENT"}}},
                {"changes": "malformed"},
            ],
        })
        self.assertEqual(set(result["history"][0]), {"at", "by", "reason", "changes"})
        self.assertEqual(result["history"][0]["by"], "unknown")
        self.assertEqual(result["history"][0]["changes"]["status"], {"from": "ABSENT", "to": "PRESENT"})
        self.assertEqual(result["history"][1]["changes"], {})


if __name__ == "__main__":
    unittest.main()
