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

from textwrap import dedent

import pretend
import pytest
import requests
import responses

from zope.interface.verify import verifyClass

from warehouse.helpdesk.interfaces import IHelpDeskService
from warehouse.helpdesk.services import ConsoleHelpDeskService, HelpScoutService


@pytest.mark.parametrize("service_class", [ConsoleHelpDeskService, HelpScoutService])
class TestHelpDeskService:
    """Common tests for the service interface."""

    def test_verify_service_class(self, service_class):
        assert verifyClass(IHelpDeskService, service_class)

    @responses.activate
    def test_create_service(self, service_class):
        responses.add(
            responses.POST,
            "https://api.helpscout.net/v2/oauth2/token",
            json={"access_token": "NOT_REAL_TOKEN"},
        )

        context = None
        request = pretend.stub(
            http=requests.Session(),
            registry=pretend.stub(
                settings={
                    "helpscout.app_id": "an insecure helpscout app id",
                    "helpscout.app_secret": "an insecure helpscout app secret",
                },
            ),
        )

        service = service_class.create_service(context, request)
        assert isinstance(service, service_class)


class TestConsoleHelpDeskService:
    def test_create_service(self):
        service = ConsoleHelpDeskService.create_service(None, None)
        assert isinstance(service, ConsoleHelpDeskService)

    def test_create_conversation(self, capsys):
        service = ConsoleHelpDeskService()

        service.create_conversation(
            request_json={
                "email": "foo@example.com",
                "name": "Foo Bar",
                "subject": "Test",
                "message": "Hello, World!",
            }
        )

        captured = capsys.readouterr()

        expected = dedent(
            """\
            Observation created
            request_json:
            {'email': 'foo@example.com',
             'message': 'Hello, World!',
             'name': 'Foo Bar',
             'subject': 'Test'}
            """
        )
        assert captured.out == expected


class TestHelpScoutService:
    @pytest.mark.parametrize(
        "partial_setting", ["helpscout.app_id", "helpscout.app_secret"]
    )
    def test_create_service_fails_when_missing_setting(self, partial_setting):
        context = None
        request = pretend.stub(
            http=pretend.stub(),
            registry=pretend.stub(settings={partial_setting: "some value"}),
        )

        with pytest.raises(KeyError):
            HelpScoutService.create_service(context, request)

    @responses.activate
    def test_create_conversation(self):
        responses.add(
            responses.POST,
            "https://api.helpscout.net/v2/conversations",
            headers={"Location": "https://api.helpscout.net/v2/conversations/123"},
            json=None,
        )

        service = HelpScoutService(
            session=requests.Session(),
            bearer_token="fake token",
            mailbox_id="12345",
        )

        resp = service.create_conversation(
            request_json={
                "email": "foo@example.com",
                "name": "Foo Bar",
                "subject": "Test",
                "message": "Hello, World!",
            }
        )

        assert resp == "https://api.helpscout.net/v2/conversations/123"
        assert len(responses.calls) == 1