import base64
import hashlib
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from orbit.config import Config
from orbit.walt import Connector, ISSUER, RESOURCE, SCOPE, WaltError, request


def token(**updates):
    return dict({'access_token':'test-access','refresh_token':'test-refresh',
                 'token_type':'Bearer','scope':SCOPE,'expires_in':3600}, **updates)


class WaltTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(Config(), db=str(Path(self.temp.name)/'orbit.sqlite3'))
        self.c = Connector(self.config)
        self.metadata = {'issuer':ISSUER, 'authorization_endpoint':ISSUER+'/oauth/v1/authorize',
            'token_endpoint':ISSUER+'/oauth/v1/token', 'registration_endpoint':ISSUER+'/oauth/v1/register',
            'code_challenge_methods_supported':['S256'], 'token_endpoint_auth_methods_supported':['none']}
        self.resource = {'resource':RESOURCE,'authorization_servers':[ISSUER],'scopes_supported':[SCOPE]}

    def tearDown(self):
        self.temp.cleanup()

    def begin(self):
        with patch('orbit.walt.request', side_effect=[(self.resource,None),(self.metadata,None),
                                                    ({'client_id':'test-client'},None)]) as network:
            result = self.c.begin()
            self.assertEqual(network.call_args_list[2].args[1]['scope'], SCOPE)
            return parse_qs(urlsplit(result['authorization_url']).query)

    def test_pkce_and_minimal_scope(self):
        params = self.begin()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.c.pending['verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'],[challenge])
        self.assertEqual(params['scope'],[SCOPE])
        self.assertEqual(params['resource'],[RESOURCE])
        self.assertEqual(params['redirect_uri'],[self.c.redirect_uri])
        self.assertNotIn('verifier', json.dumps(params))

    def test_callback_one_use_and_secret_not_in_status(self):
        params = self.begin()
        verifier = self.c.pending['verifier']
        query = 'state='+params['state'][0]+'&code=test-code'
        with patch('orbit.walt.request',return_value=(token(),None)) as network:
            self.c.callback(query)
            self.assertEqual(network.call_args.args[1]['code_verifier'],verifier)
            with self.assertRaises(WaltError):
                self.c.callback(query)
            self.assertEqual(network.call_count,1)
        self.assertTrue(self.c.status()['authorized'])
        self.assertNotIn('test-access',json.dumps(self.c.status()))
        self.assertNotIn('test-refresh',json.dumps(self.c.status()))
        self.assertTrue(Connector(self.config).status()['authorized'])

    def test_bad_and_expired_state_never_exchange_code(self):
        self.begin()
        with patch('orbit.walt.request') as network:
            with self.assertRaises(WaltError):
                self.c.callback('state=wrong&code=test-code')
            state = self.c.pending['state']
            self.c.pending['created'] -= 601
            with self.assertRaises(WaltError):
                self.c.callback('state='+state+'&code=test-code')
            network.assert_not_called()

    def test_denial_does_not_save_token(self):
        params = self.begin()
        with patch('orbit.walt.request') as network:
            with self.assertRaises(WaltError):
                self.c.callback('state='+params['state'][0]+'&error=access_denied')
            network.assert_not_called()
        self.assertFalse(self.c.status()['authorized'])
        self.assertIsNone(self.c.pending)

    def test_widened_permissions_are_rejected(self):
        with self.assertRaises(WaltError):
            self.c.accept_token(token(scope='perps.readonly perps.trade'))
        self.assertFalse(self.c.status()['authorized'])
        self.assertFalse(self.c.path.exists())

    def test_refresh_rotates_and_persists_token(self):
        self.c.saved['client_id'] = 'test-client'
        self.c.accept_token(token())
        self.c.saved['expires_at'] = time.time()-1
        with patch('orbit.walt.request',return_value=(token(access_token='new-access',refresh_token='new-refresh'),None)) as network:
            self.assertEqual(self.c.access(),'new-access')
            self.assertEqual(network.call_args.args[1]['refresh_token'],'test-refresh')
            self.assertTrue(network.call_args.kwargs['form'])
        self.assertEqual(Connector(self.config).saved['refresh_token'],'new-refresh')

    def test_discovery_rejects_changed_token_destination(self):
        self.metadata['token_endpoint'] = 'https://evil.example/token'
        with patch('orbit.walt.request',side_effect=[(self.resource,None),(self.metadata,None)]) as network:
            with self.assertRaises(WaltError):
                self.c.begin()
            self.assertEqual(network.call_count,2)

    def test_mcp_handshake_pagination_and_no_tool_execution(self):
        self.c.accept_token(token())
        responses = [({'jsonrpc':'2.0','id':1,'result':{'protocolVersion':'2025-06-18'}},'session-1'),
                     ({},None),
                     ({'jsonrpc':'2.0','id':2,'result':{'tools':[{'name':'example_read','description':'one'}], 'nextCursor':'next'}},None),
                     ({'jsonrpc':'2.0','id':3,'result':{'tools':[{'name':'example_other'}]}},None)]
        with patch('orbit.walt.request',side_effect=responses) as network:
            status = self.c.check()
            self.assertTrue(status['verified'])
            self.assertEqual(len(status['tools']),2)
            calls = network.call_args_list
            self.assertEqual(calls[1].kwargs['headers']['Mcp-Session-Id'],'session-1')
            self.assertEqual(calls[2].kwargs['bearer'],'test-access')
            self.assertEqual([c.args[1]['method'] for c in calls],['initialize','notifications/initialized','tools/list','tools/list'])
            with self.assertRaises(WaltError):
                self.c.rpc('tools/call',{'name':'example_read'},4)
            self.assertEqual(network.call_count,4)

    def test_disconnect_forgets_local_credentials(self):
        self.c.accept_token(token())
        self.c.disconnect()
        self.assertFalse(self.c.path.exists())
        self.assertFalse(self.c.status()['authorized'])
        self.assertFalse(Connector(self.config).status()['authorized'])

    def test_sse_skips_notifications_and_returns_matching_response(self):
        body = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        response = io.BytesIO(body)
        response.status = 200
        response.headers = {'Content-Type':'text/event-stream','Mcp-Session-Id':'test-session'}
        with patch('orbit.walt.build_opener') as opener:
            opener.return_value.open.return_value = response
            result, session = request(RESOURCE,{'jsonrpc':'2.0','id':1},bearer='test',rpc_id=1)
        self.assertTrue(result['result']['ok'])
        self.assertEqual(session,'test-session')

    def test_tokens_are_never_sent_to_other_hosts(self):
        with patch('orbit.walt.build_opener') as opener:
            with self.assertRaises(WaltError):
                request('https://evil.example/mcp',{},bearer='test-access')
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
