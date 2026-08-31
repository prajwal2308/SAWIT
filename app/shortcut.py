"""Build an iOS shortcut file carrying one account's key.

A shortcut shared from a phone carries whatever was typed into it, key
included, so one link cannot serve everybody — the first version of this handed
every new account write access to the owner's notes. iOS will import a shortcut
from a URL, though, so the server can generate one per account instead, with
that account's key already in place and nothing to paste.

The file is a property list of the same shape the Shortcuts app writes. It is
unsigned, which is the catch: iOS only imports unsigned shortcuts once
**Settings -> Shortcuts -> Allow Untrusted Shortcuts** is on. That is a single
switch, done once, against a key paste done inside a header field — worth it for
anyone who is not going to enjoy the second.
"""

from __future__ import annotations

import io
import plistlib

# The "Get Contents of URL" action. Everything else in a shortcut file is
# chrome around whichever actions it runs.
DOWNLOAD_URL = "is.workflow.actions.downloadurl"


def build(ingest_url: str, api_key: str, name: str = "Sawit") -> bytes:
    """One action: POST the shared link to /ingest with this account's key."""
    workflow = {
        "WFWorkflowClientVersion": "1200",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": name,
        # ActionExtension is what puts it in the share sheet at all.
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": [
            # Instagram hands over a URL sometimes and a caption containing one
            # other times; accept both and let the server find the link.
            "WFURLContentItem",
            "WFStringContentItem",
        ],
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 463140863,
            "WFWorkflowIconGlyphNumber": 59511,
        },
        # No questions on import: anything asked here is another step for
        # somebody who just wanted to save a reel.
        "WFWorkflowImportQuestions": [],
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": DOWNLOAD_URL,
                "WFWorkflowActionParameters": {
                    "WFURL": ingest_url,
                    "WFHTTPMethod": "POST",
                    "ShowHeaders": True,
                    "WFHTTPHeaders": _dictionary({
                        "X-API-Key": api_key,
                        "Content-Type": "application/json",
                    }),
                    "WFHTTPBodyType": "JSON",
                    "WFJSONValues": _json_body_with_shortcut_input("url"),
                },
            }
        ],
    }
    buffer = io.BytesIO()
    plistlib.dump(workflow, buffer, fmt=plistlib.FMT_BINARY)
    return buffer.getvalue()


def _dictionary(pairs: dict[str, str]) -> dict:
    """Shortcuts' dictionary literal. Verbose, but it is the shape it reads."""
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {
                    "WFItemType": 0,
                    "WFKey": {"Value": {"string": key}, "WFSerializationType": "WFTextTokenString"},
                    "WFValue": {
                        "Value": {"string": value},
                        "WFSerializationType": "WFTextTokenString",
                    },
                }
                for key, value in pairs.items()
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def _json_body_with_shortcut_input(field: str) -> dict:
    """A JSON body whose one field is the thing being shared.

    The value is a token rather than text — the placeholder byte with an
    attachment describing it — which is exactly what picking "Shortcut Input"
    from the variable bar produces by hand.
    """
    token = {
        "Value": {
            "string": "￼",
            "attachmentsByRange": {"{0, 1}": {"Type": "ExtensionInput"}},
        },
        "WFSerializationType": "WFTextTokenString",
    }
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {
                    "WFItemType": 0,
                    "WFKey": {
                        "Value": {"string": field},
                        "WFSerializationType": "WFTextTokenString",
                    },
                    "WFValue": token,
                }
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }
