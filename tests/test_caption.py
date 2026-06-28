"""Unit tests for the pure caption-building logic.

Run: pip install -r requirements.txt pytest && pytest
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bird_post import build_caption


def test_single_species():
    assert build_caption("Barred Owl", "Rea St.", "5-30-26", False) == (
        "Barred Owl\n\nRea St.\n\n5-30-26"
    )


def test_multiple_species_are_split_and_stripped():
    assert build_caption("Great Egret,  Osprey ", "Stevens Pond", "6-1-26", False) == (
        "Great Egret\nOsprey\n\nStevens Pond\n\n6-1-26"
    )


def test_out_of_area_prefixes_every_species_line():
    assert build_caption("Sandhill Crane, Limpkin", "Florida", "1-2-26", True) == (
        "⚠️ Sandhill Crane\n⚠️ Limpkin\n\nFlorida\n\n1-2-26"
    )
