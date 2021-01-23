import os
import os.path
import regex
import json
import requests
import requests.auth
import tarfile
import tempfile
import hashlib
import urllib.parse

from . import manifest_creator


class Registry(object):
    def __init__(
        self,
        logger,
        registry_url,
        archive_path,
        stream=False,
        login=None,
        password=None,
        ssl_verify=True,
        replace_tags_match=None,
        replace_tags_target=None,
    ):
        self._logger = logger.get_child('registry')

        # enrich http suffix if missing
        if urllib.parse.urlparse(registry_url).scheme not in ['http', 'https']:
            registry_url = 'http://' + registry_url
        self._registry_url = registry_url
        self._archive_path = os.path.abspath(archive_path)
        self._login = login
        self._password = password
        self._basicauth = None
        self._stream = stream
        self._ssl_verify = ssl_verify
        self._replace_tags_match = replace_tags_match
        self._replace_tags_target = replace_tags_target
        if self._login:
            self._basicauth = requests.auth.HTTPBasicAuth(self._login, self._password)
        self._logger.debug(
            'Initialized',
            registry_url=self._registry_url,
            archive_path=self._archive_path,
            login=self._login,
            password=self._password,
            ssl_verify=self._ssl_verify,
            stream=self._stream,
            replace_tags_match=self._replace_tags_match,
            replace_tags_target=self._replace_tags_target,
        )

    def process_archive(self):
        """
        Processing given archive and pushes the images it contains to the registry
        """
        self._logger.info('Processing archive', archive_path=self._archive_path)
        manifest_file = self._get_manifest_from_tar()[0]
        self._logger.debug('Extracted archive manifest', manifest_file=manifest_file)
        repo_tags = manifest_file["RepoTags"]
        config_loc = manifest_file["Config"]
        config_parsed = self._get_config_from_tar(config_loc)
        self._logger.verbose('Parsed config', config_parsed=config_parsed)

        with tempfile.TemporaryDirectory() as tmp_dir_name:
            for repo in repo_tags:
                image, tag = self._parse_image_tag(repo)
                self._logger.info('Extracting tar for image', image=image, tag=tag)
                self._extract_tar_file(tmp_dir_name)

                # push individual image layers
                layers = manifest_file["Layers"]
                for layer in layers:
                    self._logger.info('Pushing layer', layer=layer)
                    success, push_url = self._initialize_push(image)
                    if not success:
                        return False

                    layer_path = os.path.join(tmp_dir_name, layer)
                    self._push_layer(layer_path, push_url)

                # then, push image config
                self._logger.info(
                    'Pushing image config', image=image, config_loc=config_loc
                )
                success, push_url = self._initialize_push(image)
                if not success:
                    return False
                config_path = os.path.join(tmp_dir_name, config_loc)
                self._push_config(config_path, push_url)

                # keep the pushed layers
                properly_formatted_layers = []
                for layer in layers:
                    layer_path = os.path.join(tmp_dir_name, layer)
                    properly_formatted_layers.append(layer_path)

                # Now we need to create and push a manifest for the image
                creator = manifest_creator.ImageManifestCreator(
                    config_path, properly_formatted_layers
                )
                image_manifest = creator.create()

                # from --replace-tags-match and --replace-tags-target
                tag = self._replace_tag(image, tag)

                self._logger.info('Pushing image manifest', image=image, tag=tag)
                if not self._push_manifest(image_manifest, image, tag):
                    self._logger.info('Failed to push manifest', image=image, tag=tag)
                self._logger.info('Image Pushed', image=image, tag=tag)
        return True

    def _get_manifest_from_tar(self):
        return self._extract_json_from_tar(self._archive_path, "manifest.json")

    def _get_config_from_tar(self, name):
        return self._extract_json_from_tar(self._archive_path, name)

    def _extract_json_from_tar(self, tar_filepath, file_to_parse):
        loaded = self._extract_file_from_tar(tar_filepath, file_to_parse)
        stringified = self._parse_as_utf8(loaded)
        return json.loads(stringified)

    @staticmethod
    def _extract_file_from_tar(tar_filepath, file_to_extract):
        manifest = tarfile.open(tar_filepath)
        file_contents = manifest.extractfile(file_to_extract)
        return file_contents

    @staticmethod
    def _parse_as_utf8(to_parse):
        as_str = (to_parse.read()).decode("utf-8")
        to_parse.close()
        return as_str

    def _conditional_print(self, what, end=None):
        if self._stream:
            if end:
                print(what, end=end)
            else:
                print(what)

    def _extract_tar_file(self, tmp_dir_name):
        tar = tarfile.open(self._archive_path)
        tar.extractall(tmp_dir_name)
        tar.close()
        return True

    def _push_manifest(self, manifest, image, tag):
        headers = {
            "Content-Type": "application/vnd.docker.distribution.manifest.v2+json"
        }
        url = self._registry_url + "/v2/" + image + "/manifests/" + tag
        r = requests.put(
            url,
            headers=headers,
            data=manifest,
            auth=self._basicauth,
            verify=self._ssl_verify,
        )
        return r.status_code == 201

    def _initialize_push(self, repository):
        """
        Request a push URL for the image repository for a layer or manifest
        """
        self._logger.debug('Initializing push', repository=repository)

        response = requests.post(
            self._registry_url + "/v2/" + repository + "/blobs/uploads/",
            auth=self._basicauth,
            verify=self._ssl_verify,
        )
        upload_url = None
        if response.headers.get("Location", None):
            upload_url = response.headers.get("Location")
        success = response.status_code == 202
        if not success:
            self._logger.warn(
                'Failed to initialize push',
                status_code=response.status_code,
                contents=response.content,
            )
        return success, upload_url

    def _push_layer(self, layer_path, upload_url):
        self._chunked_upload(layer_path, upload_url)

    def _push_config(self, layer_path, upload_url):
        self._chunked_upload(layer_path, upload_url)

    def _chunked_upload(self, filepath, url):
        content_path = os.path.abspath(filepath)
        content_size = os.stat(content_path).st_size
        with open(content_path, "rb") as f:
            index = 0
            headers = {}
            upload_url = url
            sha256hash = hashlib.sha256()

            for chunk in self._read_in_chunks(f, sha256hash):
                if "http" not in upload_url:
                    upload_url = self._registry_url + upload_url
                offset = index + len(chunk)
                headers['Content-Type'] = 'application/octet-stream'
                headers['Content-Length'] = str(len(chunk))
                headers['Content-Range'] = '%s-%s' % (index, offset)
                index = offset
                last = False
                if offset == content_size:
                    last = True
                try:
                    self._conditional_print(
                        "Pushing... "
                        + str(round((offset / content_size) * 100, 2))
                        + "%  ",
                        end="\r",
                    )
                    if last:
                        digest_str = str(sha256hash.hexdigest())
                        requests.put(
                            f"{upload_url}&digest=sha256:{digest_str}",
                            data=chunk,
                            headers=headers,
                            auth=self._basicauth,
                            verify=self._ssl_verify,
                        )
                    else:
                        response = requests.patch(
                            upload_url,
                            data=chunk,
                            headers=headers,
                            auth=self._basicauth,
                            verify=self._ssl_verify,
                        )
                        if "Location" in response.headers:
                            upload_url = response.headers["Location"]

                except Exception as exc:
                    self._logger.error(
                        'Failed to upload file image upload', filepath=filepath, exc=exc
                    )
                    return False
            f.close()

        self._conditional_print("")

    # chunk size default 2T (??)
    @staticmethod
    def _read_in_chunks(file_object, hashed, chunk_size=2097152):
        while True:
            data = file_object.read(chunk_size)
            hashed.update(data)
            if not data:
                break
            yield data

    @staticmethod
    def _parse_image_tag(image_ref):

        # should be 2 parts exactly
        image, tag = image_ref.split(":")
        return image, tag

    def _replace_tag(self, image, orig_tag):
        if self._replace_tags_match and self._replace_tags_match:
            match_regex = regex.compile(self._replace_tags_match)
            if match_regex.match(orig_tag):
                self._logger.info(
                    'Replacing tag for image',
                    image=image,
                    orig_tag=orig_tag,
                    new_tag=self._replace_tags_target,
                )
                return self._replace_tags_target
            else:
                self._logger.debug(
                    'Replace tag match given but did not match',
                    image=image,
                    orig_tag=orig_tag,
                    replace_tags_match=self._replace_tags_match,
                    new_tag=self._replace_tags_target,
                )

        return orig_tag
