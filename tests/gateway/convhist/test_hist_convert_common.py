from datetime import datetime, timezone

import pytest
from yeoman_gateway.history.convert.common import LOCAL_ZONE, clean_text, epoch_or_iso_to_ms


@pytest.mark.parametrize(("raw", "text", "description", "placeholder", "changed"), [
 ("hallo", "hallo", None, None, False), (None, None, None, None, False),
 ("[Image] @203075365150770 wie belastbar", "@203075365150770 wie belastbar", None, "image", True),
 ("[Image]\n[image_description] This social media post", None, "This social media post", "image", True),
 ("[group_notes_batch] [4917623568044] [Image] [image_description] This image shows",
  "[4917623568044] [Image]", "This image shows", None, True),
 ("[group_notes_batch] [4917623568044] Und ja, zu teuer", "[4917623568044] Und ja, zu teuer", None, None, True),
 ("[Sticker]", None, None, "sticker", True),
])
def test_clean_text(raw, text, description, placeholder, changed):
 cleaned = clean_text(raw)
 assert (cleaned.text, cleaned.description, cleaned.placeholder, cleaned.changed) == (text, description, placeholder, changed)

def test_times():
 assert epoch_or_iso_to_ms("1789463229") == (1789463229000, "provider_timestamp")
 assert epoch_or_iso_to_ms(1791038794000) == (1791038794000, "provider_timestamp")
 naive = datetime(2026, 3, 18, 20, 12, 16, 589348, tzinfo=LOCAL_ZONE)
 assert epoch_or_iso_to_ms("2026-03-18T20:12:16.589348") == (int(naive.timestamp() * 1000), "capture_time_approx")
 aware = datetime(2026, 5, 28, 12, 26, 52, 418461, tzinfo=timezone.utc)
 assert epoch_or_iso_to_ms("2026-05-28T12:26:52.418461+00:00") == (int(aware.timestamp() * 1000), "capture_time_approx")
 assert epoch_or_iso_to_ms("not a time") == (None, "unknown")
 assert epoch_or_iso_to_ms(None) == (None, "unknown")
