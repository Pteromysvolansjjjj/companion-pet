"""Unit tests for the text parsers (no OCR, no audio, no game needed).

    python vision/tests/test_parse.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lolvision import parse  # noqa: E402
from lolvision.feed import EventTracker  # noqa: E402


class ClockTests(unittest.TestCase):
    def test_plain_and_fullwidth(self):
        self.assertEqual(parse.parse_clock('23:41'), 23 * 60 + 41)
        self.assertEqual(parse.parse_clock('２３：４１'), 23 * 60 + 41)

    def test_bare_digits_and_noise(self):
        self.assertEqual(parse.parse_clock('2341'), 23 * 60 + 41)
        self.assertIsNone(parse.parse_clock('99:99'))
        self.assertIsNone(parse.parse_clock('abc'))


class TopBarTests(unittest.TestCase):
    def test_split_parts(self):
        text = '15   23:41   8'
        values = parse.parse_top_bar(text)
        self.assertEqual(values['gameTime'], 23 * 60 + 41)
        self.assertEqual(values['allyKills'], 15)
        self.assertEqual(values['enemyKills'], 8)

    def test_single_number_is_not_taken_for_a_score(self):
        self.assertEqual(parse.parse_top_bar('23:41'), {'gameTime': 23 * 60 + 41})


class KillTests(unittest.TestCase):
    def test_champion_kill(self):
        event = parse.parse_kill('亚索 击杀了 劫')
        self.assertEqual(event['kind'], 'ChampionKill')
        self.assertEqual(event['killer'], '亚索')
        self.assertEqual(event['victim'], '劫')

    def test_no_space_variant(self):
        event = parse.parse_kill('李青击杀了阿狸')
        self.assertEqual((event['killer'], event['victim']), ('李青', '阿狸'))

    def test_self_death(self):
        event = parse.parse_kill('你已被 盖伦 击杀')
        self.assertEqual(event['kind'], 'SelfDeath')

    def test_objective(self):
        event = parse.parse_kill('巨龙 已被击杀')
        self.assertEqual(event['kind'], 'ObjectiveKill')

    def test_gibberish_is_ignored(self):
        self.assertIsNone(parse.parse_kill('。。'))


class BoardTests(unittest.TestCase):
    def test_kda_and_cs(self):
        board = parse.parse_board_texts(['暗裔剑魔 亚托克斯 5/2/7 182', '琴瑟仙女 娑娜 0/4/9 21'])
        self.assertEqual(board['rows'][0]['kills'], 5)
        self.assertEqual(board['rows'][0]['cs'], 182)
        self.assertEqual(board['rows'][1]['assists'], 9)

    def test_row_without_kda_stays_text(self):
        board = parse.parse_board_texts(['装备栏'])
        self.assertIsNone(board['rows'][0].get('kills'))


class TrackerTests(unittest.TestCase):
    def test_baseline_then_new_event(self):
        tracker = EventTracker()
        self.assertEqual(tracker.update(['亚索 击杀了 劫'], 100), [], 'first frame is a baseline')
        self.assertEqual(tracker.update(['亚索 击杀了 劫', '李青 击杀了 阿狸'], 110)[0]['KillerName'], '李青')
        self.assertEqual(len(tracker.update(['亚索 击杀了 劫', '李青 击杀了 阿狸'], 120)), 0, 'rows still on screen')

    def test_same_pair_twice_is_two_events(self):
        tracker = EventTracker()
        tracker.update([], 0)
        events = tracker.update(['亚索 击杀了 劫', '亚索 击杀了 劫'], 200)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]['EventID'], events[1]['EventID'])

    def test_event_id_survives_while_visible(self):
        tracker = EventTracker()
        tracker.update([], 0)
        first = tracker.update(['亚索 击杀了 劫'], 300)[0]
        second = tracker.update(['亚索 击杀了 劫'], 305)
        self.assertEqual(second, [])
        self.assertEqual(first['EventID'], tracker.active['kill:亚索>劫']['id'])

    def test_row_reappearing_after_retirement_is_new(self):
        tracker = EventTracker()
        tracker.update([], 0)
        first = tracker.update(['亚索 击杀了 劫'], 400)[0]
        for _ in range(3):
            tracker.update([], 405)
        second = tracker.update(['亚索 击杀了 劫'], 600)[0]
        self.assertNotEqual(first['EventID'], second['EventID'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
