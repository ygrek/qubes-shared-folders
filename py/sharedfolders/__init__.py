#!/usr/bin/python3

from __future__ import annotations

import base64
import errno
import glob
import hashlib
import json
import logging
import os
import subprocess
import sys
from typing import Literal, Optional, Tuple, Dict, TypeVar, Type, Any


class Response(object):
    def __init__(self, name: str):
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return "<Response " + self.name + " %s>" % id(self)

    def is_allow(self) -> bool:
        return self.name.startswith("ALLOW")

    def is_onetime(self) -> bool:
        return self.name.endswith("ONETIME")

    def is_block(self) -> bool:
        return self.name == "BLOCK"

    @staticmethod
    def from_string(string: str) -> Response:
        global RESPONSES
        try:
            r = RESPONSES.__dict__[string]
            assert isinstance(r, Response)
            return r
        except KeyError:
            raise ValueError(string)


class RESPONSES(object):
    ALLOW_ONETIME = Response("ALLOW_ONETIME")
    DENY_ONETIME = Response("DENY_ONETIME")
    ALLOW_ALWAYS = Response("ALLOW_ALWAYS")
    DENY_ALWAYS = Response("DENY_ALWAYS")
    BLOCK = Response("BLOCK")


logger = logging.getLogger(__name__)


def contains(needle: str, haystack: str) -> bool:
    """Check if the needle path is contained in the haystack path."""
    needle = os.path.abspath(needle)
    haystack = os.path.abspath(haystack)
    if not needle.endswith(os.path.sep):
        needle = needle + os.path.sep
    if not haystack.endswith(os.path.sep):
        haystack = haystack + os.path.sep
    if needle == haystack:
        return True
    if (needle).startswith(haystack):
        return True
    return False


def fingerprint_decision(source: str, target: str, folder: str) -> str:
    fingerprint = hashlib.sha256()
    fingerprint.update(source.encode("utf-8"))
    fingerprint.update(b"\0")
    fingerprint.update(target.encode("utf-8"))
    fingerprint.update(b"\0")
    fingerprint.update(folder.encode("utf-8"))
    fingerprint.update(b"\0")
    return fingerprint.hexdigest()[:32]


class Decision(object):
    source: str = ""
    target: str = ""
    folder: str = ""
    response: Response = Response("invalid")

    def __init__(self, source: str, target: str, folder: str, response: Response):
        self.source = source
        self.target = target
        self.folder = folder
        self.response = response


class DecisionMatrix(Dict[str, Decision]):
    POLICY_DB = "/etc/qubes/shared-folders/policy.db"

    @classmethod
    def load(klass: Type[DecisionMatrix]) -> DecisionMatrix:
        def hook(obj: Dict[Any, Any]) -> Any:
            if "folder" in obj:
                return Decision(
                    source=obj["source"],
                    target=obj["target"],
                    folder=obj["folder"],
                    response=Response.from_string(obj["response"]),
                )
            else:
                return DecisionMatrix(obj)

        try:
            with open(klass.POLICY_DB, "r") as db:
                data = json.load(db, object_hook=hook)
            self = klass()
            for k, v in data.items():
                self[k] = v
            return self
        except Exception:
            return klass()

    def save(self) -> None:
        class DecisionMatrixEncoder(json.JSONEncoder):
            def default(self, obj: Any) -> Any:
                if isinstance(obj, Decision):
                    return obj.__dict__
                if isinstance(obj, Response):
                    return str(obj)
                return json.JSONEncoder.default(self, obj)

        with open(self.POLICY_DB + ".tmp", "w") as db:
            json.dump(self, db, indent=4, sort_keys=True, cls=DecisionMatrixEncoder)
        os.chmod(self.POLICY_DB + ".tmp", 0o664)
        os.rename(self.POLICY_DB + ".tmp", self.POLICY_DB)

    def revoke_onetime_accesses_for_fingerprint(self, fingerprint: str) -> None:
        """This method mutates the internal state and updates the policy on disk."""
        if fingerprint in self and self[fingerprint].response.is_onetime():
            logger.info(
                "One-time decision expired for %s, applying policy changes", fingerprint
            )
            del self[fingerprint]
            ConnectToFolderPolicy.apply_policy_changes_from(self)
            self.save()

    def lookup_decision(
        self, source: str, target: str, folder: str
    ) -> Tuple[Optional[Decision], str]:
        """Look up a decision in the table for src->dst VMs, from most specific to least specific.

        If no decision is made, prospectively generate a fingerprint for this decision to use later.
        """
        matches = []
        for fingerprint, decision in self.items():
            if (
                source == decision.source
                and target == decision.target
                and contains(folder, decision.folder)
            ):
                matches.append((fingerprint, decision))
        if matches:
            for fingerprint, match in reversed(
                sorted(matches, key=lambda m: len(m[1].folder))
            ):
                if match.response.is_allow():
                    break
            return match, fingerprint
        fingerprint = fingerprint_decision(source, target, folder)
        return None, fingerprint

    def lookup_prior_authorization(
        self, source: str, target: str, folder: str
    ) -> Tuple[Optional[Response], str]:
        """Called by the client qube during AuthorizeFolderAccess before
        process_authorization_request.

        This method mutates the internal state and updates the policy on disk."""
        match, fingerprint = self.lookup_decision(source, target, folder)
        self.revoke_onetime_accesses_for_fingerprint(fingerprint)
        return (
            (match.response, fingerprint)
            if (match and not match.response.is_onetime())
            else (None, fingerprint)
        )

    def process_authorization_request(
        self, source: str, target: str, folder: str, response: Response
    ) -> str:
        """Called by the client qube during AuthorizeFolderAccess, after
        lookup_prior_authorization.

        This method mutates the internal state and updates the policy on disk."""
        if response.is_block():
            # The user means to block ALL, so we transform this into
            # a complete block for the machine, by blocking the root
            # directory, which means to block absolutely everything.
            response = RESPONSES.DENY_ALWAYS
            folder = "/"
        fingerprint = fingerprint_decision(source, target, folder)
        decision = Decision(source, target, folder, response)
        self[fingerprint] = decision
        ConnectToFolderPolicy.apply_policy_changes_from(self)
        self.save()
        return fingerprint

    def lookup_decision_folder(
        self, fingerprint: str, requested_folder: str
    ) -> Optional[str]:
        """Called by the server qube during ConnectToFolder to verify the
        folder is a subfolder or is the same as the folder the client qube
        is authorized to access.

        The client qube has already connected to the server qube here.

        This method mutates the internal state and updates the policy on disk."""
        match = self.get(fingerprint)
        self.revoke_onetime_accesses_for_fingerprint(fingerprint)
        if match and contains(requested_folder, match.folder):
            logger.info(
                "Requested folder %s is contained in folder %s", requested_folder, match
            )
            return match.folder
        else:
            logger.info("No approved requests for folder %s", requested_folder)
            return None


class _ConnectToFolderPolicy(object):

    FNTPL = "/etc/qubes-rpc/policy/ruddo.ConnectToFolder+%s"

    def ctf_policy(self, fingerprint: str) -> str:
        return self.FNTPL % fingerprint

    def grant_for(self, source: str, target: str, fingerprint: str) -> None:
        fn = self.ctf_policy(fingerprint)
        if os.path.isfile(fn):
            return
        logger.info("Creating %s", fn)
        with open(fn + ".tmp", "w") as f:
            f.write("%s %s allow" % (source, target))
        os.chmod(fn + ".tmp", 0o664)
        os.rename(fn + ".tmp", fn)

    def revoke_for(
        self, unused_source: str, unused_target: str, fingerprint: str
    ) -> None:
        fn = self.ctf_policy(fingerprint)
        try:
            os.unlink(fn)
            logger.info("Removing %s", fn)
        except FileNotFoundError:
            pass

    def apply_policy_changes_from(self, matrix: DecisionMatrix) -> None:
        existing_policy_files = glob.glob(self.FNTPL % "*")
        for fingerprint, decision in matrix.items():
            if self.FNTPL % fingerprint in existing_policy_files:
                existing_policy_files.remove(self.FNTPL % fingerprint)
            action = self.grant_for if decision.response.is_allow() else self.revoke_for
            action(decision.source, decision.target, fingerprint)
        for p in existing_policy_files:
            logger.info("Removing %s", p)
            os.unlink(p)


ConnectToFolderPolicy = _ConnectToFolderPolicy()
