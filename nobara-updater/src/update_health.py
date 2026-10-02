"""Check the complete proposed RPM set without installing or running scripts."""
from __future__ import annotations

from collections import defaultdict
import logging

import rpm
import libdnf5.transaction as trans

from .update_state import UpdateError

LOG = logging.getLogger(__name__)
OUTBOUND = {trans.TransactionItemAction_REMOVE, trans.TransactionItemAction_REPLACED}


def full_nevra(header):
    return header.sprintf("%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}")


class PackageHealth:
    def __init__(self, base):
        self.root = base.get_config().get_installroot_option().get_value()
        self.installonly = set(base.get_config().get_installonlypkgs_option().get_value())
        self.repair_packages = []
        ts = rpm.TransactionSet(self.root)
        try:
            iterator = ts.dbMatch()
            # Include excluded and locally protected RPMs too. They remain
            # installed and must participate in dependency validation.
            self.installed = {iterator.instance(): header for header in iterator if header["arch"]}
        finally:
            ts.closeDB()

    def check(self, headers, removed=()):
        # A fresh transaction is necessary: librpm caches dependency results
        # within a transaction set. Never reuse one after adding repairs.
        ts = rpm.TransactionSet(self.root)
        try:
            for offset in removed:
                ts.addErase(offset)
            for header in headers:
                ts.addInstall(header, None, "i")
            ts.check()
            return [problem for problem in ts.problems()
                    if problem.type != rpm.RPMPROB_OBSOLETES
                    and not (problem.type == rpm.RPMPROB_CONFLICT and problem.pkgNEVR == problem.altNEVR)]
        finally:
            ts.closeDB()

    def removed(self, transaction):
        return {item.get_package().get_rpmdbid() for item in transaction.get_transaction_packages()
                if item.get_action() in OUTBOUND}

    def repair_requirements(self, transaction):
        """Re-resolve requirements of broken RPMs that will remain installed.

        Distro-sync alone does not revisit dependencies of unchanged RPMs.
        Let DNF solve these requirements (including rich dependencies) rather
        than selecting a provider or architecture ourselves. Requirements of
        an RPM already being replaced must not pull in its obsolete ABI.
        """
        problems = self.check(self.installed.values())
        broken = {problem.altNEVR for problem in problems if problem.type == rpm.RPMPROB_REQUIRES}
        removed = self.removed(transaction)
        requirements = set()
        for offset, header in self.installed.items():
            if offset in removed or header.sprintf("%{NEVRA}") not in broken:
                continue
            LOG.warning("Repairing missing dependencies of %s within the prepared update.", full_nevra(header))
            self.repair_packages.append(full_nevra(header))
            for dependency in rpm.ds(header, rpm.RPMTAG_REQUIRENAME):
                requirement = dependency.DNEVR()[2:]  # Strip the RPM dependency kind, "R ".
                if not requirement.startswith("rpmlib("):
                    requirements.add(requirement)
        return requirements

    def validate(self, transaction, payloads):
        removed = self.removed(transaction)
        headers = [header for offset, header in self.installed.items() if offset not in removed]
        ts = rpm.TransactionSet(self.root)
        try:
            # DNF has already verified every downloaded signature. Here we
            # read those same RPM headers solely for dependency checking.
            ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
            for payload in payloads:
                with payload.open("rb") as stream:
                    headers.append(ts.hdrFromFdno(stream))
        finally:
            ts.closeDB()
        errors = {str(problem) for problem in self.check(headers, removed)}
        identities = defaultdict(list)
        for header in headers:
            if self.installonly.intersection(header[rpm.RPMTAG_PROVIDENAME]):
                continue
            identities[(header["name"], header["arch"])].append(full_nevra(header))
        for versions in identities.values():
            if len(versions) > 1:
                errors.add("Duplicate installed versions would remain: " + ", ".join(sorted(versions)))
        if errors:
            raise UpdateError("The prepared update would leave package problems:\n" + "\n".join(sorted(errors))
                              + "\nNo packages have been changed. Repair the reported packages or their repositories, then retry.")
