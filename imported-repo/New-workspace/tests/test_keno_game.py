import asyncio
import unittest

import keno_game


class KenoMathTests(unittest.TestCase):
    def test_exact_hit_probabilities_sum_to_one(self):
        for spots in range(1, 11):
            probability = sum(
                keno_game.hit_probability(spots, hits)
                for hits in range(0, min(spots, 10) + 1)
            )
            self.assertAlmostEqual(probability, 1.0, places=12)

    def test_draw_is_ten_unique_numbers_in_range(self):
        draw = keno_game.generate_draw()
        self.assertEqual(len(draw), 10)
        self.assertEqual(len(set(draw)), 10)
        self.assertTrue(all(1 <= number <= 40 for number in draw))

    def test_payout_tables_are_deterministic_and_valid(self):
        for mode in keno_game.KENO_CONFIG["modes"]:
            spots = 10
            first = keno_game.build_payout_table(spots, mode)
            second = keno_game.build_payout_table(spots, mode)
            self.assertEqual(first, second)
            self.assertEqual(first[0], 0.0)
            self.assertTrue(all(value >= 0 for value in first.values()))
            self.assertTrue(
                all(value <= keno_game.KENO_MAX_PAYOUT_MULTIPLIER for value in first.values())
            )
            for hits, multiplier in first.items():
                if multiplier > 0:
                    self.assertGreaterEqual(
                        multiplier,
                        keno_game.KENO_CONFIG["modes"][mode].base_multiplier,
                    )
                    self.assertGreater(
                        keno_game.hit_probability(spots, hits), 0
                    )

    def test_invalid_payout_inputs_do_not_pay(self):
        self.assertEqual(keno_game.payout_multiplier(0, 0, "medium"), 0.0)
        self.assertEqual(keno_game.payout_multiplier(9, 9, "medium"), 0.0)
        self.assertEqual(keno_game.payout_multiplier(11, 11, "medium"), 0.0)
        self.assertEqual(keno_game.payout_multiplier(5, 6, "medium"), 0.0)

    def test_keno_requires_exactly_ten_numbers(self):
        self.assertEqual(keno_game.KENO_CONFIG["minimum_selection"], 10)
        self.assertEqual(keno_game.KENO_CONFIG["maximum_selection"], 10)


class KenoStateTests(unittest.TestCase):
    def setUp(self):
        keno_game.restore_state({})

    def tearDown(self):
        keno_game.restore_state({})

    def test_state_round_trip_preserves_independent_sessions(self):
        session_id, session = keno_game._new_session("123456", 77, 5.0)
        session["selected_numbers"] = [1, 4, 10]
        session["status"] = "setup"
        saved = keno_game.export_state()
        keno_game.restore_state(saved)
        restored = keno_game._get_owned_session(session_id, "123456")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["selected_numbers"], [1, 4, 10])
        self.assertEqual(restored["game"], "keno")

    def test_restore_state_rejects_invalid_numbers_and_statuses(self):
        keno_game.restore_state({
            "valid": {
                "game": "keno",
                "status": "setup",
                "selected_numbers": [1, 1, 40, 41, "bad", -2],
                "draw_numbers": [2, 2, 41, 3],
                "revealed_numbers": [3, 40, 2],
                "hit_numbers": [1, 3, 999],
            },
            "invalid": {
                "game": "keno",
                "status": "not-a-status",
            },
        })

        restored = keno_game._sessions["valid"]
        self.assertEqual(restored["selected_numbers"], [1, 40])
        self.assertEqual(restored["draw_numbers"], [2, 3])
        self.assertEqual(restored["revealed_numbers"], [3, 2])
        self.assertEqual(restored["hit_numbers"], [])
        self.assertNotIn("invalid", keno_game._sessions)

    def test_interrupted_debit_is_refunded_once_on_recovery(self):
        credited = []

        class FakeServices:
            def credit_balance(self, user_id, amount, transaction_type):
                credited.append((user_id, amount, transaction_type))
                return amount

            def save(self):
                return None

            class logger:
                @staticmethod
                def exception(*args, **kwargs):
                    pass

        original = keno_game._services
        try:
            keno_game.configure(FakeServices())
            session_id, session = keno_game._new_session("123456", 77, 5.0)
            session["status"] = "revealing"
            session["bet_debited"] = True
            self.assertEqual(keno_game.recover_incomplete_sessions(), 1)
            self.assertEqual(keno_game.recover_incomplete_sessions(), 0)
            self.assertEqual(credited, [("123456", 5.0, "keno_recovery_refund")])
            self.assertEqual(
                keno_game._get_owned_session(session_id, "123456")["status"],
                "finished",
            )
        finally:
            keno_game._services = original


if __name__ == "__main__":
    unittest.main()