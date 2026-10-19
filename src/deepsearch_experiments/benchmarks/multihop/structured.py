"""Conservative JSON envelope extraction; never repair JSON or choose an answer."""
from dataclasses import dataclass
import json
import math
import re


@dataclass(frozen=True)
class StructuredObject:
    value: dict | None = None
    envelope: str | None = None
    error_code: str | None = None


class InvalidJSON(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidJSON('duplicate_key')
        result[key] = value
    return result


def _constant(_):
    raise InvalidJSON('nonfinite_number')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise InvalidJSON('nonfinite_number')
    return number


def parse_object(text: str) -> StructuredObject:
    if not isinstance(text, str) or not text.strip():
        return StructuredObject(error_code='empty_response')
    if len(text) > 131072:
        return StructuredObject(error_code='response_too_large')
    text = text.strip()
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    envelope = 'plain'
    try:
        if text.startswith(('{', '[')):
            value = decoder.decode(text)
        elif '```' in text:
            if text.count('```') != 2:
                return StructuredObject(error_code='ambiguous_or_invalid_fence')
            match = re.search(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```', text, re.DOTALL | re.IGNORECASE)
            if match is None:
                return StructuredObject(error_code='invalid_fence')
            outside = text[:match.start()] + text[match.end():]
            if '{' in outside or '}' in outside:
                return StructuredObject(error_code='ambiguous_objects')
            value = decoder.decode(match.group(1).strip())
            envelope = 'fenced_with_prose' if outside.strip() else 'fenced'
        else:
            start = text.find('{')
            if start < 0:
                return StructuredObject(error_code='no_json_object')
            value, end = decoder.raw_decode(text, start)
            prefix, suffix = text[:start], text[end:]
            # Do not extract an inner object from an array or malformed outer JSON.
            if any(mark in prefix + suffix for mark in '{}[]'):
                return StructuredObject(error_code='ambiguous_objects')
            envelope = 'prose'
        if not isinstance(value, dict):
            return StructuredObject(error_code='non_object')
        pending = [(value, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 64:
                return StructuredObject(error_code='nesting_too_deep')
            children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
            pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
        return StructuredObject(value=value, envelope=envelope)
    except InvalidJSON as exc:
        return StructuredObject(error_code=str(exc))
    except (ValueError, RecursionError):
        return StructuredObject(error_code='invalid_json')
