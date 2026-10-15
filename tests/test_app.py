import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app as module


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = patch.object(module, 'ROOT', Path(self.tmp.name))
        self.root.start()
        module.jobs.clear()
        module.auth = module.InstagramAuth()
        self.client = module.app.test_client()

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def seed(self):
        module.jobs['test'] = dict(username='example', status='running', images=0,
                                   processed=0, total=0, message='')

    def post(self, code, video=False, nodes=None):
        return SimpleNamespace(
            typename='GraphSidecar' if nodes is not None else 'GraphImage',
            is_video=video, shortcode=code, date_utc=datetime(2026, 1, 1),
            url='https://example.invalid/photo.jpg',
            get_sidecar_nodes=lambda: iter(nodes),
        )

    def test_archive_includes_carousel_photos_and_skips_videos(self):
        self.seed()
        nodes = [SimpleNamespace(is_video=False, display_url='photo'),
                 SimpleNamespace(is_video=True, display_url='video')]
        posts = [self.post('single'), self.post('carousel', nodes=nodes), self.post('video', video=True)]
        profile = SimpleNamespace(is_private=False, mediacount=3, get_posts=lambda: iter(posts))
        with patch.object(module.instaloader, 'Instaloader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            factory.return_value.download_pic.side_effect = lambda stem, url, date: Path(stem + '.jpg').write_bytes(b'photo')
            module.archive_photos('test', 'example')
        job = module.jobs['test']
        self.assertEqual(job['status'], 'done')
        self.assertEqual((job['images'], job['processed']), (2, 3))
        with zipfile.ZipFile(Path(self.tmp.name) / 'test/photos.zip') as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertTrue(all(name.startswith('example/') for name in archive.namelist()))
            self.assertTrue(all(archive.read(name) == b'photo' for name in archive.namelist()))
        self.assertEqual(len(list((Path(self.tmp.name) / 'test').iterdir())), 1)
        response = self.client.get('/api/jobs/test/download')
        self.assertEqual(response.status_code, 200)
        self.assertIn('example_photos.zip', response.headers['Content-Disposition'])
        response.close()

    def test_media_selection_filters_single_posts_and_mixed_carousels(self):
        for media_type, expected in [('photos', (2, 0)), ('videos', (0, 2)), ('both', (2, 2))]:
            with self.subTest(media_type=media_type):
                self.seed()
                module.jobs['test']['media_type'] = media_type
                nodes = [SimpleNamespace(is_video=False, display_url='photo.jpg'),
                         SimpleNamespace(is_video=True, video_url='carousel.mp4')]
                video = self.post('video', video=True)
                video.video_url = 'single.mp4'
                posts = [self.post('single'), video, self.post('carousel', nodes=nodes)]
                profile = SimpleNamespace(is_private=False, mediacount=3, get_posts=lambda: iter(posts))
                def save(stem, url, date):
                    Path(stem + ('.mp4' if url.endswith('.mp4') else '.jpg')).write_bytes(url.encode())
                with patch.object(module, 'new_loader') as factory, patch.object(
                    module.instaloader.Profile, 'from_username', return_value=profile
                ):
                    factory.return_value.download_pic.side_effect = save
                    module.archive_photos('test', 'example', media_type=media_type)
                job = module.jobs['test']
                self.assertEqual(job['status'], 'done')
                self.assertEqual((job['images'], job.get('videos', 0)), expected)
                self.assertEqual(job['processed'], 3)
                with zipfile.ZipFile(Path(self.tmp.name) / 'test/photos.zip') as archive:
                    names = archive.namelist()
                    self.assertEqual(sum(n.endswith('.jpg') for n in names), expected[0])
                    self.assertEqual(sum(n.endswith('.mp4') for n in names), expected[1])
                    for name in names:
                        self.assertTrue(archive.read(name))
                response = self.client.get('/api/jobs/test/download')
                suffix = 'media' if media_type == 'both' else media_type
                self.assertIn(f'example_{suffix}.zip', response.headers['Content-Disposition'])
                response.close()
                import shutil
                shutil.rmtree(Path(self.tmp.name) / 'test')

    def test_invalid_media_selection_is_rejected(self):
        for value in ['invalid', '', None, [], {}]:
            response = self.client.post('/api/jobs', json={'username': 'example', 'media_type': value})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(module.jobs, {})

    def test_cancel_during_transfer_removes_files_and_stops_next_media(self):
        self.seed()
        profile = SimpleNamespace(is_private=False, mediacount=2,
                                  get_posts=lambda: iter([self.post('first'), self.post('second')]))
        def save(stem, url, date):
            Path(stem + '.jpg').write_bytes(b'partial')
            response = self.client.post('/api/jobs/test/cancel', json={})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['cancel_requested'])
        with patch.object(module, 'new_loader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            factory.return_value.download_pic.side_effect = save
            module.archive_photos('test', 'example')
            self.assertEqual(factory.return_value.download_pic.call_count, 1)
        self.assertEqual(module.jobs['test']['status'], 'cancelled')
        self.assertIn('finished', module.jobs['test'])
        self.assertFalse((Path(self.tmp.name) / 'test').exists())
        self.assertEqual(self.client.get('/api/jobs/test/download').status_code, 404)
        with patch.object(module.threading, 'Thread'):
            self.assertEqual(self.client.post('/api/jobs', json={'username': 'next'}).status_code, 202)

    def test_cancel_before_worker_starts_and_repeated_cancel(self):
        self.seed()
        for _ in range(2):
            self.assertEqual(self.client.post('/api/jobs/test/cancel', json={}).status_code, 200)
        with patch.object(module, 'new_loader') as factory:
            module.archive_photos('test', 'example')
            factory.assert_not_called()
        self.assertEqual(module.jobs['test']['status'], 'cancelled')
        self.assertEqual(self.client.post('/api/jobs/missing/cancel', json={}).status_code, 404)
        module.jobs['test']['status'] = 'done'
        self.assertEqual(self.client.post('/api/jobs/test/cancel', json={}).json['status'], 'done')

    def test_story_and_highlight_sources_and_media_filters(self):
        import shutil
        from itertools import combinations
        for size in (1, 2, 3):
            for sources in combinations(('posts', 'stories', 'highlights'), size):
                for media_type in ('photos', 'videos', 'both'):
                    with self.subTest(sources=sources, media_type=media_type):
                        self.seed()
                        def story_item(mediaid, video):
                            return SimpleNamespace(mediaid=mediaid, is_video=video,
                                                   date_utc=datetime(2026, 1, 1),
                                                   url='story.jpg', video_url='story.mp4')
                        photo, video = story_item(1, False), story_item(2, True)
                        story = SimpleNamespace(get_items=lambda: iter([photo, video]))
                        highlight = SimpleNamespace(unique_id=42, title="Summer [2026]", get_items=lambda: iter([photo, video]))
                        post_video = self.post('video', video=True)
                        post_video.video_url = 'post.mp4'
                        profile = SimpleNamespace(is_private=False, userid=123, mediacount=2,
                                                  get_posts=Mock(side_effect=lambda: iter([self.post('photo'), post_video])))
                        def save(stem, url, date):
                            Path(stem + ('.mp4' if url.endswith('.mp4') else '.jpg')).write_bytes(b'media')
                        with patch.object(module, 'new_loader') as factory, patch.object(
                            module.instaloader.Profile, 'from_username', return_value=profile
                        ):
                            loader = factory.return_value
                            loader.download_pic.side_effect = save
                            loader.get_stories.return_value = iter([story])
                            loader.get_highlights.return_value = iter([highlight])
                            module.archive_photos('test', 'example', ('viewer', {}), media_type, list(sources))
                            self.assertEqual(module.jobs['test']['status'], 'done')
                            self.assertEqual(profile.get_posts.call_count, int('posts' in sources))
                            if 'stories' in sources:
                                loader.get_stories.assert_called_once_with(userids=[123])
                            else:
                                loader.get_stories.assert_not_called()
                            if 'highlights' in sources:
                                loader.get_highlights.assert_called_once_with(profile)
                            else:
                                loader.get_highlights.assert_not_called()
                        with zipfile.ZipFile(Path(self.tmp.name) / 'test/photos.zip') as archive:
                            names = archive.namelist()
                            self.assertEqual(len(names), len(sources) * (2 if media_type == 'both' else 1))
                            self.assertEqual(len(names), len(set(names)))
                            for name in names:
                                media_folder = 'Videos' if name.endswith('.mp4') else 'Photos'
                                self.assertEqual(Path(name).parts[-2], media_folder)
                            for source in sources:
                                self.assertTrue(any(n.startswith(f'example/{source}/') for n in names))
                            if 'highlights' in sources:
                                self.assertTrue(any(n.startswith('example/highlights/Summer [2026]/') for n in names))
                            if media_type != 'both':
                                self.assertTrue(all(n.endswith('.jpg' if media_type == 'photos' else '.mp4') for n in names))
                        shutil.rmtree(Path(self.tmp.name) / 'test')

    def test_highlight_folder_names_are_safe_and_unique(self):
        used = set()
        for title, expected in [
            ('Travel', 'Travel'), ('travel', 'travel (2)'),
            ('Travel (2)', 'Travel (2) (2)'), ('../Trip/Photos?', '.._Trip_Photos_'),
            ('CON', '_CON'), ('NUL.txt', '_NUL.txt'), ('...', 'Untitled'),
            ('', 'Untitled (2)'), (' Summer [2026] ', 'Summer [2026]'),
        ]:
            with self.subTest(title=title):
                self.assertEqual(module.highlight_folder(title, used), expected)
        self.assertLessEqual(len(module.highlight_folder('x' * 300, used)), 80)

    def test_source_validation_and_story_login_requirement(self):
        for sources in ([], None, 'stories', ['bad'], [123], [{}]):
            self.assertEqual(self.client.post('/api/jobs', json={'username': 'example', 'sources': sources}).status_code, 400)
        for sources in (['stories'], ['highlights'], ['posts', 'stories', 'highlights']):
            self.assertEqual(self.client.post('/api/jobs', json={'username': 'example', 'sources': sources}).status_code, 401)
        self.assertEqual(module.jobs, {})
        module.auth.username = 'viewer'
        module.auth.session = {'sessionid': 'fake'}
        with patch.object(module.threading, 'Thread') as worker:
            response = self.client.post('/api/jobs', json={'username': 'example', 'sources': ['stories', 'highlights']})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(worker.call_args.kwargs['args'][4], ['stories', 'highlights'])

    def test_profile_preview_and_avatar_failure(self):
        import base64
        profile = SimpleNamespace(profile_pic_url='https://example.invalid/avatar.jpg')
        with patch.object(module, 'new_loader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            response = Mock()
            response.headers = {'Content-Type': 'image/jpeg'}
            response.iter_content.return_value = [b'avatar']
            factory.return_value.context.get_raw.return_value = response
            result = self.client.post('/api/profile', json={'username': '@example'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['username'], 'example')
            self.assertEqual(result.json['avatar'], 'data:image/jpeg;base64,' + base64.b64encode(b'avatar').decode())
            response.close.assert_called_once()
            factory.return_value.context.get_raw.side_effect = module.instaloader.ConnectionException()
            result = self.client.post('/api/profile', json={'username': 'example'})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('avatar', result.json)
        self.assertEqual(self.client.post('/api/profile', json={'username': '../bad'}).status_code, 400)
        with patch.object(module.instaloader.Profile, 'from_username', side_effect=module.instaloader.ProfileNotExistsException()):
            self.assertEqual(self.client.post('/api/profile', json={'username': 'missing'}).status_code, 502)

    def test_cancel_during_story_download(self):
        self.seed()
        item = SimpleNamespace(mediaid=1, is_video=True, video_url='story.mp4', date_utc=datetime(2026, 1, 1))
        profile = SimpleNamespace(is_private=False, userid=123, mediacount=0)
        with patch.object(module, 'new_loader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            factory.return_value.get_stories.return_value = [SimpleNamespace(get_items=lambda: iter([item, item]))]
            def save(stem, url, date):
                Path(stem + '.mp4').write_bytes(b'partial')
                self.client.post('/api/jobs/test/cancel', json={})
            factory.return_value.download_pic.side_effect = save
            module.archive_photos('test', 'example', ('viewer', {}), 'both', ['stories'])
            self.assertEqual(factory.return_value.download_pic.call_count, 1)
        self.assertEqual(module.jobs['test']['status'], 'cancelled')
        self.assertFalse((Path(self.tmp.name) / 'test').exists())

    def test_failure_removes_partial_archive(self):
        self.seed()
        def posts():
            yield self.post('first')
            raise module.instaloader.ConnectionException('blocked')
        profile = SimpleNamespace(is_private=False, mediacount=2, get_posts=posts)
        with patch.object(module.instaloader, 'Instaloader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            factory.return_value.download_pic.side_effect = lambda stem, url, date: Path(stem + '.jpg').write_bytes(b'photo')
            module.archive_photos('test', 'example')
        self.assertEqual(module.jobs['test']['status'], 'error')
        self.assertFalse((Path(self.tmp.name) / 'test').exists())
        self.assertEqual(self.client.get('/api/jobs/test/download').status_code, 404)

    def test_validation_and_origin(self):
        self.assertEqual(self.client.post('/api/jobs', json=['name']).status_code, 400)
        for value in ['../escape', 'https://instagram.com/name', '', ['name']]:
            self.assertEqual(self.client.post('/api/jobs', json={'username': value}).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json={'username': 'name'}, headers={'Origin': 'https://other.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/jobs/missing').status_code, 404)

    def test_job_creation_and_concurrency_limit(self):
        with patch.object(module.threading, 'Thread') as thread:
            response = self.client.post('/api/jobs', json={'username': '@example'})
            self.assertEqual(response.status_code, 202)
            thread.return_value.start.assert_called_once()
            job = self.client.get('/api/jobs/' + response.json['id'])
            self.assertEqual(job.json['username'], 'example')
            self.assertEqual(self.client.post('/api/jobs', json={'username': 'other'}).status_code, 409)

    def test_page(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="login-form"', page.data)
        self.assertIn(b'id="two-factor-form"', page.data)
        self.assertIn(b'type="password"', page.data)
        response = self.client.get('/static/app.js')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_setup_failures_release_the_download_slot(self):
        for target in ('directory', 'loader'):
            with self.subTest(target=target):
                self.seed()
                failure = patch.object(Path, 'mkdir', side_effect=OSError('disk unavailable')) if target == 'directory' else patch.object(module, 'new_loader', side_effect=OSError('setup failed'))
                with failure:
                    module.archive_photos('test', 'example')
                self.assertEqual(module.jobs['test']['status'], 'error')
                self.assertIn('finished', module.jobs['test'])

    def test_worker_start_failure_does_not_block_future_downloads(self):
        with patch.object(module.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('thread unavailable')
            response = self.client.post('/api/jobs', json={'username': 'example'})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(any(job['status'] == 'running' for job in module.jobs.values()))

    def test_missing_zip_returns_a_helpful_error(self):
        self.seed()
        module.jobs['test']['status'] = 'done'
        response = self.client.get('/api/jobs/test/download')
        self.assertEqual(response.status_code, 410)
        self.assertIn('error', response.json)

    def test_own_private_profile_is_accessible(self):
        self.seed()
        profile = SimpleNamespace(is_private=True, followed_by_viewer=False,
                                  username='EXAMPLE', mediacount=1,
                                  get_posts=lambda: iter([self.post('mine')]))
        with patch.object(module, 'new_loader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', return_value=profile
        ):
            factory.return_value.context.username = 'example'
            factory.return_value.download_pic.side_effect = lambda stem, url, date: Path(stem + '.jpg').write_bytes(b'photo')
            module.archive_photos('test', 'example')
        self.assertEqual(module.jobs['test']['status'], 'done')

    def test_cleanup_failure_still_sets_finished(self):
        self.seed()
        with patch.object(module, 'new_loader') as factory, patch.object(
            module.instaloader.Profile, 'from_username', side_effect=module.instaloader.ConnectionException()
        ):
            factory.return_value.close.side_effect = OSError('close failed')
            module.archive_photos('test', 'example')
        self.assertEqual(module.jobs['test']['status'], 'error')
        self.assertIn('finished', module.jobs['test'])

    def test_download_snapshots_login_when_submitted(self):
        module.auth.username = 'original'
        module.auth.session = {'csrftoken': 'fake', 'sessionid': 'original-cookie'}
        with patch.object(module.threading, 'Thread') as thread:
            response = self.client.post('/api/jobs', json={'username': 'example'})
            self.assertEqual(response.status_code, 202)
            snapshot = thread.call_args.kwargs['args'][2]
        module.auth.logout()
        self.assertEqual(snapshot, ('original', {'csrftoken': 'fake', 'sessionid': 'original-cookie'}))
        self.assertNotIn('cookie', self.client.get('/api/jobs/' + response.json['id']).get_data(as_text=True))

    def test_download_during_login_is_rejected_without_stuck_job(self):
        with module.auth.lock:
            response = self.client.post('/api/jobs', json={'username': 'example'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(module.jobs, {})

    def test_oversized_json_returns_json_error(self):
        response = self.client.post('/api/jobs', json={'username': 'x' * 5000})
        self.assertEqual(response.status_code, 413)
        self.assertIn('error', response.json)

    def test_private_profile_and_empty_profile(self):
        for private in (True, False):
            with self.subTest(private=private):
                self.seed()
                profile = SimpleNamespace(is_private=private, followed_by_viewer=False,
                                          mediacount=0, get_posts=lambda: iter([]))
                with patch.object(module.instaloader, 'Instaloader'), patch.object(
                    module.instaloader.Profile, 'from_username', return_value=profile
                ):
                    module.archive_photos('test', 'example')
                self.assertEqual(module.jobs['test']['status'], 'error')
                self.assertFalse((Path(self.tmp.name) / 'test').exists())


if __name__ == '__main__':
    unittest.main()
