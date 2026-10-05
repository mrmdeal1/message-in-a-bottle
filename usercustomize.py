try:
    import storage
except Exception:
    storage = None


TEST_REPLY_MESSAGE = "Your bottle found me at just the right time. Thank you for sending it into the world."


if storage is not None:
    _reply_test_original_load_bottle = storage.load_bottle

    def load_bottle_with_test_reply(account_id=None, month_key=None):
        bottle = _reply_test_original_load_bottle(account_id, month_key)

        if (
            bottle is not None
            and isinstance(account_id, str)
            and account_id.startswith("ios-test-")
            and not bottle.get("reply_message")
        ):
            bottle["reply_message"] = TEST_REPLY_MESSAGE
            bottle["reply_time"] = bottle.get("current_time")
            try:
                storage.save_bottle(bottle)
            except Exception as exc:
                print(f"Test reply save skipped: {exc}")

        return bottle

    storage.load_bottle = load_bottle_with_test_reply


try:
    import build4_support  # noqa: F401
except Exception as exc:
    print(f"Build 4 support import skipped: {exc}")
