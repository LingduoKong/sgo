import unittest
from unittest.mock import Mock, patch
from web.auth import ses_sender

class EmailDeliveryTests(unittest.TestCase):
    def test_reply_address_is_separate_from_authenticated_sender(self):
        client = Mock()
        with patch.dict('os.environ', {'AWS_REGION':'us-west-2', 'SES_FROM_EMAIL':'login@example.com', 'SES_REPLY_TO_EMAIL':'support@example.com'}), patch('boto3.client', return_value=client):
            ses_sender('recipient@example.com', '12345678')
        payload = client.send_email.call_args.kwargs
        self.assertEqual(payload['FromEmailAddress'], 'login@example.com')
        self.assertEqual(payload.get('ReplyToAddresses'), ['support@example.com'])
