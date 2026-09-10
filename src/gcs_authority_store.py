"""GCS JSON API adapter using an injected requests-shaped transport only.

No credentials, session construction, retry, or cloud calls occur at import.
Caller supplies the exact bucket/object and a transport with retries disabled.
Reads select metadata generation first, then pin media to that exact generation;
a missing pinned version is a read error, never permission to initialize.
"""

from urllib.parse import quote

from processing_state import (Mutation, Observation, encode, load_authority, require,
                              text, validate_authority)


class StoreReadError(RuntimeError):
    pass


def generation(value):
    # An opaque server token: syntax checked, never incremented or ordered locally.
    require(isinstance(value, str) and value.isascii() and value.isdigit()
            and value != "0" and not value.startswith("0"), "invalid server generation")
    return value


class GCSAuthorityStore:
    def __init__(self, *, transport, bucket, object_name, feed):
        for value in (bucket, object_name, feed):
            text(value)
        self.transport, self.bucket, self.object_name, self.feed = transport, bucket, object_name, feed
        self.url = ("https://storage.googleapis.com/storage/v1/b/" + quote(bucket, safe="")
                    + "/o/" + quote(object_name, safe=""))
        self.upload_url = ("https://storage.googleapis.com/upload/storage/v1/b/"
                           + quote(bucket, safe="") + "/o")

    def _request(self, method, url, deadline, **kwargs):
        require(type(deadline) in (float, int) and deadline > 0, "request deadline required")
        return self.transport.request(method, url, timeout=deadline, allow_redirects=False, **kwargs)

    def _metadata(self, response):
        value = response.json()
        require(isinstance(value, dict) and value.get("bucket") == self.bucket
                and value.get("name") == self.object_name, "wrong GCS target metadata")
        return generation(value.get("generation"))

    def read(self, deadline):
        try:
            response = self._request("GET", self.url, deadline,
                                     params={"fields": "bucket,name,generation"})
            if response.status_code == 404:
                return None
            require(response.status_code == 200, "authority metadata read failed")
            token = self._metadata(response)
            response = self._request("GET", self.url, deadline,
                                     params={"alt": "media", "generation": token})
            require(response.status_code == 200, "pinned authority media read failed")
            header = response.headers.get("x-goog-generation")
            require(header is None or header == token, "media generation mismatch")
            return Observation(load_authority(response.content, self.feed), token)
        except Exception as error:
            raise StoreReadError("authority read unresolved; not absent") from error

    def write(self, record, expected_generation, deadline):
        validate_authority(record, self.feed)
        token = "0" if expected_generation is None else generation(expected_generation)
        payload = encode(record)
        try:
            response = self._request(
                "POST", self.upload_url, deadline,
                params={"uploadType": "media", "name": self.object_name, "ifGenerationMatch": token},
                headers={"Content-Type": "application/json"}, data=payload)
        except Exception:
            return Mutation("unknown", reason="authority_response_unknown")
        if response.status_code == 412:
            return Mutation("conflict", reason="generation_precondition_failed")
        if response.status_code in (400, 401, 403, 404, 405, 413, 415, 422):
            return Mutation("rejected", reason="authority_request_rejected")
        if response.status_code not in (200, 201):
            return Mutation("unknown", reason="authority_response_unknown")
        try:
            token = self._metadata(response)
        except Exception:
            return Mutation("unknown", reason="authority_success_response_malformed")
        return Mutation("success", generation=token)
