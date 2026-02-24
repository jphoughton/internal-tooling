"""Unit tests for the RateLimiter class in rate_limiter.py."""
import time
import unittest
from unittest.mock import patch

from rate_limiter import RateLimiter


class TestRateLimiterAllowsWithinLimit(unittest.TestCase):
    def test_first_request_is_allowed(self):
        rl = RateLimiter(max_requests=60, window_seconds=60)
        self.assertTrue(rl.is_allowed('1.2.3.4'))

    def test_requests_up_to_limit_are_allowed(self):
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            self.assertTrue(rl.is_allowed('1.2.3.4'))

    def test_different_ips_have_independent_counters(self):
        rl = RateLimiter(max_requests=1, window_seconds=60)
        self.assertTrue(rl.is_allowed('1.1.1.1'))
        self.assertTrue(rl.is_allowed('2.2.2.2'))

    def test_limit_of_one_allows_exactly_one_request(self):
        rl = RateLimiter(max_requests=1, window_seconds=60)
        self.assertTrue(rl.is_allowed('1.2.3.4'))
        self.assertFalse(rl.is_allowed('1.2.3.4'))


class TestRateLimiterRejectsOverLimit(unittest.TestCase):
    def test_request_beyond_limit_is_rejected(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            rl.is_allowed('1.2.3.4')
        self.assertFalse(rl.is_allowed('1.2.3.4'))

    def test_multiple_excess_requests_are_all_rejected(self):
        rl = RateLimiter(max_requests=2, window_seconds=60)
        rl.is_allowed('1.2.3.4')
        rl.is_allowed('1.2.3.4')
        self.assertFalse(rl.is_allowed('1.2.3.4'))
        self.assertFalse(rl.is_allowed('1.2.3.4'))
        self.assertFalse(rl.is_allowed('1.2.3.4'))

    def test_rejection_does_not_increment_counter(self):
        # A rejected request should not count toward the window, so the
        # total allowed after the window resets is still max_requests.
        rl = RateLimiter(max_requests=2, window_seconds=60)
        rl.is_allowed('1.2.3.4')
        rl.is_allowed('1.2.3.4')
        # Rejected calls — counter must stay at 2
        rl.is_allowed('1.2.3.4')
        rl.is_allowed('1.2.3.4')
        count, _ = rl._windows['1.2.3.4']
        self.assertEqual(count, 2)


class TestRateLimiterWindowReset(unittest.TestCase):
    def test_window_resets_after_expiry(self):
        rl = RateLimiter(max_requests=2, window_seconds=1)
        rl.is_allowed('1.2.3.4')
        rl.is_allowed('1.2.3.4')
        self.assertFalse(rl.is_allowed('1.2.3.4'))

        # Advance monotonic clock past the window
        now = time.monotonic()
        with patch('rate_limiter.time') as mock_time:
            mock_time.monotonic.return_value = now + 2
            self.assertTrue(rl.is_allowed('1.2.3.4'))

    def test_new_window_starts_fresh_after_expiry(self):
        rl = RateLimiter(max_requests=3, window_seconds=1)
        for _ in range(3):
            rl.is_allowed('1.2.3.4')

        now = time.monotonic()
        with patch('rate_limiter.time') as mock_time:
            mock_time.monotonic.return_value = now + 2
            # Three more requests should all be allowed in the new window
            for _ in range(3):
                self.assertTrue(rl.is_allowed('1.2.3.4'))

    def test_request_within_same_window_is_still_limited(self):
        rl = RateLimiter(max_requests=2, window_seconds=60)
        rl.is_allowed('1.2.3.4')
        rl.is_allowed('1.2.3.4')

        now = time.monotonic()
        with patch('rate_limiter.time') as mock_time:
            # Still inside the 60-second window
            mock_time.monotonic.return_value = now + 30
            self.assertFalse(rl.is_allowed('1.2.3.4'))


class TestRateLimiterDefaults(unittest.TestCase):
    def test_default_limit_is_60_requests(self):
        rl = RateLimiter()
        for _ in range(60):
            self.assertTrue(rl.is_allowed('1.2.3.4'))
        self.assertFalse(rl.is_allowed('1.2.3.4'))

    def test_default_window_is_60_seconds(self):
        rl = RateLimiter()
        self.assertEqual(rl._window_seconds, 60)


if __name__ == '__main__':
    unittest.main()
