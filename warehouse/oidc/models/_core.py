# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, Unpack

import rfc3986
import sentry_sdk

from sigstore.verify.policy import VerificationPolicy
from sqlalchemy import ForeignKey, String, orm
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from warehouse import db
from warehouse.oidc.errors import InvalidPublisherError, ReusedTokenError
from warehouse.oidc.interfaces import SignedClaims

if TYPE_CHECKING:
    from warehouse.accounts.models import User
    from warehouse.macaroons.models import Macaroon
    from warehouse.oidc.services import OIDCPublisherService
    from warehouse.packaging.models import Project

C = TypeVar("C")


class CheckNamedArguments(TypedDict, total=False):
    publisher_service: OIDCPublisherService


CheckClaimCallable = Callable[[C, C, SignedClaims, Unpack[CheckNamedArguments]], bool]


def check_claim_binary(binary_func: Callable[[C, C], bool]) -> CheckClaimCallable[C]:
    """
    Wraps a binary comparison function so that it takes three arguments instead,
    ignoring the third.

    This is used solely to make claim verification compatible with "trivial"
    comparison checks like `str.__eq__`.
    """

    def wrapper(
        ground_truth: C,
        signed_claim: C,
        _all_signed_claims: SignedClaims,
        **_kwargs: Unpack[CheckNamedArguments],
    ) -> bool:
        return binary_func(ground_truth, signed_claim)

    return wrapper


def check_claim_invariant(value: C) -> CheckClaimCallable[C]:
    """
    Wraps a fixed value comparison into a three-argument function.

    This is used solely to make claim verification compatible with "invariant"
    comparison checks, like "claim x is always the literal `true` value".
    """

    def wrapper(
        ground_truth: C,
        signed_claim: C,
        _all_signed_claims: SignedClaims,
        **_kwargs: Unpack[CheckNamedArguments],
    ):
        return ground_truth == signed_claim == value

    return wrapper


def check_existing_jti(
    _ground_truth,
    signed_claim,
    _all_signed_claims,
    **kwargs: Unpack[CheckNamedArguments],
) -> bool:
    """Returns True if the checks passes or raises an exception."""

    publisher_service: OIDCPublisherService = kwargs["publisher_service"]

    if publisher_service.jwt_identifier_exists(signed_claim):
        publisher_service.metrics.increment(
            "warehouse.oidc.reused_token",
            tags=[f"publisher:{publisher_service.publisher}"],
        )
        raise ReusedTokenError()

    return True


class OIDCPublisherProjectAssociation(db.Model):
    __tablename__ = "oidc_publisher_project_association"

    oidc_publisher_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oidc_publishers.id"),
        nullable=False,
        primary_key=True,
    )
    project_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )


class OIDCPublisherMixin:
    """
    A mixin for common functionality between all OIDC publishers, including
    "pending" publishers that don't correspond to an extant project yet.
    """

    # Each hierarchy of OIDC publishers (both `OIDCPublisher` and
    # `PendingOIDCPublisher`) use a `discriminator` column for model
    # polymorphism, but the two are not mutually polymorphic at the DB level.
    discriminator = mapped_column(String)

    # A map of claim names to "check" functions, each of which
    # has the signature `check(ground-truth, signed-claim, all-signed-claims) -> bool`.
    __required_verifiable_claims__: dict[str, CheckClaimCallable[Any]] = dict()

    # A set of claim names which must be present, but can't be verified
    __required_unverifiable_claims__: set[str] = set()

    # Simlar to __verificable_claims__, but these claims are optional
    __optional_verifiable_claims__: dict[str, CheckClaimCallable[Any]] = dict()

    # Claims that have already been verified during the JWT signature
    # verification phase if present.
    __preverified_claims__ = {
        "iss",
        "iat",
        "nbf",
        "exp",
        "aud",
    }

    # Individual publishers should explicitly override this set,
    # indicating any custom claims that are known to be present but are
    # not checked as part of verifying the JWT.
    __unchecked_claims__: set[str] = set()

    # Individual publishers can have complex unique constraints on their
    # required and optional attributes, and thus can't be naively looked
    # up from a raw claim set.
    #
    # Each subclass should explicitly override this list to contain
    # class methods that take a `SignedClaims` and return a SQLAlchemy
    # expression that, when queried, should produce exactly one or no result.
    # This list should be ordered by specificity, e.g. selecting for the
    # expression with the most optional constraints first, and ending with
    # the expression with only required constraints.
    #
    # TODO(ww): In principle this list is computable directly from
    # `__required_verifiable_claims__` and `__optional_verifiable_claims__`,
    # but there are a few problems: those claim sets don't map to their
    # "equivalent" column (only to an instantiated property), and may not
    # even have an "equivalent" column.
    __lookup_strategies__: list = []

    @classmethod
    def lookup_by_claims(cls, session, signed_claims: SignedClaims):
        for lookup in cls.__lookup_strategies__:
            query = lookup(cls, signed_claims)
            if not query:
                # We might not build a query if we know the claim set can't
                # satisfy it. If that's the case, then we skip.
                continue

            if publisher := query.with_session(session).one_or_none():
                return publisher
        raise InvalidPublisherError("All lookup strategies exhausted")

    @classmethod
    def all_known_claims(cls) -> set[str]:
        """
        Returns all claims "known" to this publisher.
        """
        return (
            cls.__required_verifiable_claims__.keys()
            | cls.__required_unverifiable_claims__
            | cls.__optional_verifiable_claims__.keys()
            | cls.__preverified_claims__
            | cls.__unchecked_claims__
        )

    def verify_claims(
        self, signed_claims: SignedClaims, publisher_service: OIDCPublisherService
    ):
        """
        Given a JWT that has been successfully decoded (checked for a valid
        signature and basic claims), verify it against the more specific
        claims of this publisher.
        """

        # Defensive programming: treat the absence of any claims to verify
        # as a failure rather than trivially valid.
        if not self.__required_verifiable_claims__:
            raise InvalidPublisherError("No required verifiable claims")

        # All claims should be accounted for.
        # The presence of an unaccounted claim is not an error, only a warning
        # that the JWT payload has changed.
        unaccounted_claims = sorted(
            list(signed_claims.keys() - self.all_known_claims())
        )
        if unaccounted_claims:
            with sentry_sdk.push_scope() as scope:
                scope.fingerprint = unaccounted_claims
                sentry_sdk.capture_message(
                    f"JWT for {self.__class__.__name__} has unaccounted claims: "
                    f"{unaccounted_claims}"
                )

        # Finally, perform the actual claim verification. First, verify that
        # all required claims are present.
        for claim_name in (
            self.__required_verifiable_claims__.keys()
            | self.__required_unverifiable_claims__
        ):
            # All required claims are mandatory. The absence of a missing
            # claim *is* an error with the JWT, since it indicates a breaking
            # change in the JWT's payload.
            signed_claim = signed_claims.get(claim_name)
            if signed_claim is None:
                with sentry_sdk.push_scope() as scope:
                    scope.fingerprint = [claim_name]
                    sentry_sdk.capture_message(
                        f"JWT for {self.__class__.__name__} is missing claim: "
                        f"{claim_name}"
                    )
                raise InvalidPublisherError(f"Missing claim {claim_name!r}")

        # Now that we've verified all claims are present, verify each
        # verifiable claim is correct
        for claim_name, check in self.__required_verifiable_claims__.items():
            signed_claim = signed_claims.get(claim_name)
            if not check(
                getattr(self, claim_name),
                signed_claim,
                signed_claims,
                publisher_service=publisher_service,
            ):
                raise InvalidPublisherError(
                    f"Check failed for required claim {claim_name!r}"
                )

        # Check optional verifiable claims
        for claim_name, check in self.__optional_verifiable_claims__.items():
            # All optional claims are optional. The absence of a missing
            # claim is *NOT* an error with the JWT, however we should still
            # verify this against the check, because the claim might be
            # required for a given publisher.
            signed_claim = signed_claims.get(claim_name)

            if not check(
                getattr(self, claim_name),
                signed_claim,
                signed_claims,
                publisher_service=publisher_service,
            ):
                raise InvalidPublisherError(
                    f"Check failed for optional claim {claim_name!r}"
                )

        return True

    @property
    def publisher_name(self) -> str:  # pragma: no cover
        # Only concrete subclasses are constructed.
        raise NotImplementedError

    @property
    def publisher_base_url(self) -> str | None:  # pragma: no cover
        # Only concrete subclasses are constructed.
        raise NotImplementedError

    def publisher_url(
        self, claims: SignedClaims | None = None
    ) -> str | None:  # pragma: no cover
        """
        NOTE: This is **NOT** a `@property` because we pass `claims` to it.
        When calling, make sure to use `publisher_url()`
        """
        # Only concrete subclasses are constructed.
        raise NotImplementedError

    def publisher_verification_policy(
        self, claims: SignedClaims
    ) -> VerificationPolicy:  # pragma: no cover
        """
        Get the policy used to verify attestations signed with this publisher.
        NOTE: This is **NOT** a `@property` because we pass `claims` to it.
        When calling, make sure to use `publisher_verification_policy()`
        """
        # Only concrete subclasses are constructed.
        raise NotImplementedError

    def stored_claims(
        self, claims: SignedClaims | None = None
    ) -> dict:  # pragma: no cover
        """
        These are claims that are serialized into any macaroon generated for
        this publisher. You likely want to use this to surface claims that
        are not configured on the publishers, that might vary from one publish
        event to the next, and are useful to show to the user.

        NOTE: This is **NOT** a `@property` because we pass `claims` to it.
        When calling, make sure to use `stored_claims()`
        """
        # Only concrete subclasses are constructed.
        raise NotImplementedError

    def verify_url(self, url: str) -> bool:
        """
        Verify a given URL against this Trusted Publisher's base URL

        A URL is considered "verified" iff it matches the Trusted Publisher URL
        such that, when both URLs are normalized:
        - The scheme component is the same (e.g: both use `https`)
        - The authority component is the same (e.g.: `github.com`)
        - The path component is the same, or a sub-path of the Trusted Publisher URL
          (e.g.: `org/project` and `org/project/issues.html` will pass verification
          against an `org/project` Trusted Publisher path component)
        - The path component of the Trusted Publisher URL is not empty
        Note: We compare the authority component instead of the host component because
        the authority includes the host, and in practice neither URL should have user
        nor port information.
        """
        publisher_uri = rfc3986.api.uri_reference(self.publisher_base_url).normalize()
        user_uri = rfc3986.api.uri_reference(url).normalize()
        if publisher_uri.path is None:
            # Currently no Trusted Publishers have an empty path component,
            # so we defensively fail verification.
            return False
        elif user_uri.path and publisher_uri.path:
            is_subpath = (
                publisher_uri.path == user_uri.path
                or user_uri.path.startswith(publisher_uri.path + "/")
            )
        else:
            is_subpath = publisher_uri.path == user_uri.path

        return (
            publisher_uri.scheme == user_uri.scheme
            and publisher_uri.authority == user_uri.authority
            and is_subpath
        )


class OIDCPublisher(OIDCPublisherMixin, db.Model):
    __tablename__ = "oidc_publishers"

    projects: Mapped[list[Project]] = orm.relationship(
        secondary=OIDCPublisherProjectAssociation.__table__,
        back_populates="oidc_publishers",
    )
    macaroons: Mapped[list[Macaroon]] = orm.relationship(
        cascade="all, delete-orphan", lazy=True
    )

    __mapper_args__ = {
        "polymorphic_identity": "oidc_publishers",
        "polymorphic_on": OIDCPublisherMixin.discriminator,
    }


class PendingOIDCPublisher(OIDCPublisherMixin, db.Model):
    """
    A "pending" OIDC publisher, i.e. one that's been registered by a user
    but doesn't correspond to an existing PyPI project yet.
    """

    __tablename__ = "pending_oidc_publishers"

    project_name = mapped_column(String, nullable=False)
    added_by_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    added_by: Mapped[User] = orm.relationship(back_populates="pending_oidc_publishers")

    __mapper_args__ = {
        "polymorphic_identity": "pending_oidc_publishers",
        "polymorphic_on": OIDCPublisherMixin.discriminator,
    }

    def reify(self, session):  # pragma: no cover
        """
        Return an equivalent "normal" OIDC publisher model for this pending publisher,
        deleting the pending publisher in the process.
        """

        # Only concrete subclasses are constructed.
        raise NotImplementedError
