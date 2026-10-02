"""EventPublisher tests: delivery guarantees requested from RabbitMQ (pika is mocked)."""
from unittest import mock

import pika
import pytest

from app.events import EventPublisher


@pytest.fixture
def channel():
    with mock.patch("app.events.pika.BlockingConnection") as connection_cls:
        yield connection_cls.return_value.channel.return_value


def test_publish_uses_confirms_and_mandatory_routing(channel):
    EventPublisher("amqp://guest:guest@localhost/%2F", "shopflow.events").publish("order.created", {"id": 1})

    channel.confirm_delivery.assert_called_once()
    kwargs = channel.basic_publish.call_args.kwargs
    assert kwargs["mandatory"] is True
    assert kwargs["routing_key"] == "order.created"
    assert kwargs["properties"].delivery_mode == 2  # persistent


def test_unroutable_event_raises_instead_of_being_dropped(channel):
    channel.basic_publish.side_effect = pika.exceptions.UnroutableError([])

    with pytest.raises(pika.exceptions.UnroutableError):
        EventPublisher("amqp://guest:guest@localhost/%2F", "shopflow.events").publish("order.created", {"id": 1})
