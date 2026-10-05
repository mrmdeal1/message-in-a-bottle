import builtins

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


# usercustomize runs before Render's installed dependencies are available to the
# application import. Defer Build 4 support until FastAPI itself has finished
# importing, then install the Build 4 FastAPI initializer before server.py
# creates its app instance.
_original_import = builtins.__import__
_build4_loaded = False
_build4_loading = False


def _import_with_build4(name, globals=None, locals=None, fromlist=(), level=0):
    global _build4_loaded, _build4_loading

    module = _original_import(name, globals, locals, fromlist, level)

    if (
        not _build4_loaded
        and not _build4_loading
        and (name == "fastapi" or name.startswith("fastapi."))
    ):
        _build4_loading = True
        try:
            _original_import("build4_support", globals, locals, (), 0)
            _build4_loaded = True
            builtins.__import__ = _original_import
            print("Build 4 support loaded after FastAPI import.")
        except Exception as exc:
            print(f"Deferred Build 4 support import skipped: {exc}")
        finally:
            _build4_loading = False

    return module


builtins.__import__ = _import_with_build4
