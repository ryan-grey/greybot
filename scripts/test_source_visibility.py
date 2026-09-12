"""A quiet raid week must not look like broken Warcraft Logs access."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import handler
import wcl


class VisibilityTests(unittest.TestCase):
    def test_old_report_proves_access_without_entering_announcement_window(self):
        old = {'code': 'old', 'startTime': 100}
        with patch.object(wcl, 'reports_in_window', side_effect=[([], {'spent': 1}), ([old], {'spent': 2})]) as query:
            recent, visible, rate = handler.probe_report_visibility('test', 1, 1000, 2000, True)
        self.assertEqual(recent, [])
        self.assertEqual(visible, [old])
        self.assertEqual(rate, {'spent': 2})
        self.assertEqual(query.call_args.args, ('test', 1, 0, 2000))
        self.assertEqual(query.call_args.kwargs, {'limit': 1})

    def test_empty_history_remains_blind(self):
        with patch.object(wcl, 'reports_in_window', return_value=([], None)) as query:
            recent, visible, _ = handler.probe_report_visibility('test', 1, 1000, 2000, True)
        self.assertEqual((recent, visible), ([], []))
        self.assertEqual(query.call_count, 2)

    def test_recent_reports_or_unchecked_team_do_not_need_history(self):
        for recent, check in (([{'code': 'recent'}], True), ([], False)):
            with patch.object(wcl, 'reports_in_window', return_value=(recent, None)) as query:
                self.assertEqual(handler.probe_report_visibility('test', 1, 1000, 2000, check)[1], recent)
                query.assert_called_once()

    def test_history_error_is_not_an_empty_success(self):
        with patch.object(wcl, 'reports_in_window', side_effect=[([], None), wcl.WCLError('temporarily unavailable')]):
            with self.assertRaises(wcl.WCLError):
                handler.probe_report_visibility('test', 1, 1000, 2000, True)


if __name__ == '__main__':
    unittest.main()
