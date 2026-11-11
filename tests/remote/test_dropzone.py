"""Dolphin URI-list reception, without taking over the user's pointer."""
import json
import unittest
from unittest import mock

from jarvis import dropzone


class DropZoneUriTests(unittest.TestCase):
    def test_local_uri_paths_decode_spaces_unicode_and_comments(self):
        data = '# Dolphin\r\nfile:///home/u/a%20b.txt\r\nfile://localhost/tmp/caf%C3%A9.txt\r\n'
        self.assertEqual(dropzone.uri_paths(data.encode()),
                         ['/home/u/a b.txt', '/tmp/café.txt'])

    def test_remote_urls_invalid_paths_and_duplicates_are_not_forwarded(self):
        data = (b'https://example.com/a\nfile://other-host/etc/passwd\n'
                b'file:relative\nfile:///tmp/a%00b\nfile:///tmp/a?query\n'
                b'file:///tmp/ok\nfile:///tmp/ok\n')
        self.assertEqual(dropzone.uri_paths(data), ['/tmp/ok'])

    def receive(self, chunks, error=None):
        stream = mock.Mock()
        emitted = mock.Mock()
        offered = mock.Mock()
        offered.read_finish.return_value = (stream, 'text/uri-list')

        def read(_size, _priority, _cancel, callback):
            stream.read_bytes_finish.return_value.get_data.return_value = next(chunks)
            callback(stream, object())

        stream.read_bytes_async.side_effect = read
        if error:
            stream.read_bytes_finish.side_effect = error
        offered.read_async.side_effect = lambda _types, _priority, _cancel, cb: cb(offered, object())
        dropzone.receive_drop(offered, 1, emit=emitted)
        return offered, stream, emitted

    def test_chunked_uri_list_is_handed_off_before_copy_acknowledgement(self):
        offered, stream, emitted = self.receive(iter([b'file:///tmp/a%20', b'b.txt\r\n', b'']))
        self.assertEqual(json.loads(emitted.call_args.args[0]), {'files': ['/tmp/a b.txt']})
        offered.finish.assert_called_once_with(1)
        stream.close.assert_called_once_with(None)
        self.assertEqual(offered.read_async.call_args.args[0], ['text/uri-list'])

    def test_failed_read_never_reports_copy_success(self):
        with mock.patch.object(dropzone.sys, 'stderr'):
            offered, stream, emitted = self.receive(iter([b'ignored']), OSError('read failed'))
        emitted.assert_not_called()
        offered.finish.assert_called_once_with(0)
        stream.close.assert_called_once()

    def test_oversized_list_is_bounded_and_rejected(self):
        with mock.patch.object(dropzone, 'MAX_URI_BYTES', 4), \
                mock.patch.object(dropzone.sys, 'stderr'):
            offered, stream, emitted = self.receive(iter([b'12345']))
        emitted.assert_not_called()
        offered.finish.assert_called_once_with(0)
        stream.close.assert_called_once()
