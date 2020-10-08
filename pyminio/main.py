from __future__ import annotations

import re

from io import BytesIO
from datetime import datetime
from types import FunctionType
from dataclasses import dataclass
from typing import List, Dict, Any
from os.path import join, basename, dirname, normpath

import pytz
from cached_property import cached_property
from minio import Minio, definitions
from attrdict import AttrDict
from minio.error import NoSuchKey, BucketNotEmpty

from .exceptions import DirectoryNotEmptyError

ROOT = "/"


@dataclass
class ObjectData:
    name: str
    full_path: str
    metadata: Dict[str, Any]


@dataclass
class File(ObjectData):
    data: bytes


@dataclass
class Folder(ObjectData):
    pass


class Match:
    PATH_STRUCTURE = \
        re.compile(r"/(?P<bucket>.*?)/+(?P<prefix>.*/+)?(?P<filename>.+[^/])?")

    def __init__(self, path: str):
        self._path = path
        self._match = self._get_match()

    @cached_property
    def path(self):
        return re.sub(r'/+', r'/', self._path)

    @property
    def bucket(self):
        return self._match.bucket

    @property
    def prefix(self):
        return self._match.prefix

    @property
    def filename(self):
        return self._match.filename

    def _get_match(self) -> AttrDict:
        """Get the bucket name, path prefix and file's name from path."""
        if self.is_root():
            return AttrDict(bucket='', prefix='', filename='')

        match = self.PATH_STRUCTURE.match(self.path)

        if match is None:
            raise ValueError(f'{self.path} is not a valid path')

        return AttrDict(
            bucket=match.group("bucket"),
            prefix=match.group("prefix") or '',
            filename=match.group("filename") or ''
        )

    def is_root(self):
        return self.path == ROOT

    @property
    def relative_path(self):
        return join(self.prefix, self.filename)

    def is_bucket(self):
        return self.relative_path == ''

    def is_dir(self):
        return self.filename == ''

    def is_file(self):
        return not self.is_dir()

    @classmethod
    def infer_operation_destination(cls, src: Match, dst: Match) -> Match:
        """Return a match with the dst path and filename if exists.
        If not, return dst path with src filename.

        Examples:
            >>> src = Match('/foo/bar1/baz')
            >>> dst = Match('/foo/bar2/')
            >>> Match.infer_file_operation_destination(src, dst)
            Match('/foo/bar2/baz')

            >>> src = Match('/foo/bar1/baz')
            >>> dst = Match('/foo/bar2/baz2')
            >>> Match.infer_file_operation_destination(src, dst)
            Match('/foo/bar2/baz2')

        Raises:
            ValueError: If src was not a valid file match.
        """
        if not src.is_file():
            raise ValueError('Src must be a valid match to a file')

        if dst.is_file():
            return dst

        else:
            return Match(join(dst.path, src.filename))


def _validate_directory(func):
    """Check if directory path is valid. """
    def decorated_method(self, path: str, *args, **kwargs):
        match = Match(path)
        if match.is_file():
            raise ValueError(f"{path} is not a valid directory path."
                             " must be absolute and end with /")

        return func(self, path, *args, **kwargs)

    return decorated_method


def get_last_modified(obj):
    """Return object's last modified time. """
    if obj.last_modified is None:
        return pytz.UTC.localize(datetime.fromtimestamp(0))

    return obj.last_modified


def get_creation_date(obj):
    """Return object's creation date. """
    if obj.creation_date is None:
        return pytz.UTC.localize(datetime.fromtimestamp(0))
    return obj.creation_date


class Pyminio:
    """Pyminio is an os-like cover to minio."""
    def __init__(self, minio_obj: Minio):
        self.minio_obj = minio_obj

    @classmethod
    def from_credentials(
        cls,
        endpoint: str,
        access_key: str,
        secret_key: str,
        **kwargs
    ) -> Pyminio:
        return cls(minio_obj=Minio(endpoint=endpoint,
                                   access_key=access_key,
                                   secret_key=secret_key,
                                   **kwargs))

    @_validate_directory
    def mkdirs(self, path: str):
        """Create path of directories.

        Works like linux's: 'mkdir -p'.

        Args:
            path: The absolute path to create.
        """
        match = Match(path)

        if match.is_root():
            raise ValueError("cannot create / directory")

        #  make bucket
        if not self.minio_obj.bucket_exists(bucket_name=match.bucket):
            self.minio_obj.make_bucket(bucket_name=match.bucket)

        if match.is_bucket():
            return

        # TODO: check if all directories has metadata and stuff.
        #  make sub directories (minio is making all path)
        empty_file = BytesIO()
        self.minio_obj.put_object(bucket_name=match.bucket,
                                  object_name=match.prefix,
                                  data=empty_file, length=0)

    def _get_objects_at(self, match: Match) -> List[definitions.Object]:
        """Return all objects in the specified bucket and directory path.

        Args:
            bucket: The bucket desired in minio.
            directory_path: full directory path inside the bucket.
        """
        return sorted(self.minio_obj.list_objects(bucket_name=match.bucket,
                                                  prefix=match.prefix),
                      key=get_last_modified, reverse=True)

    def _get_buckets(self):
        """Return all existed buckets. """
        return sorted(self.minio_obj.list_buckets(),
                      key=get_creation_date, reverse=True)

    @classmethod
    def _extract_metadata(cls, detailed_metadata: Dict):
        """Remove 'X-Amz-Meta-' from all the keys, and lowercase them.
            When metadata is pushed in the minio, the minio is adding
            those details that screw us. this is an unscrewing function.
        """
        detailed_metadata = detailed_metadata or {}
        return {key.replace('X-Amz-Meta-', '').lower(): value
                for key, value in detailed_metadata.items()}

    @_validate_directory
    def listdir(self, path: str, only_files: bool = False) -> List[str]:
        """Return all files and directories absolute paths
            within the directory path.

        Works like os.listdir, just only with absolute path.

        Args:
            path: path of a directory.
            only_files: return only files name and not directories.

        Returns:
            files and directories in path.
        """
        match = Match(path)

        if match.is_root():
            if only_files:
                return []

            return [f"{b.name}/" for b in self._get_buckets()]

        return [obj.object_name.replace(match.prefix, '')
                for obj in self._get_objects_at(match)
                if not only_files or not obj.is_dir]

    def exists(self, path: str) -> bool:
        """Check if the specified path exists.

        Works like os.path.exists.
        """
        try:
            match = Match(path)

        except ValueError:
            return False

        if match.is_root():
            return True

        bucket_exists = self.minio_obj.bucket_exists(match.bucket)
        if not bucket_exists:
            return False

        if match.is_bucket():
            return True
        try:
            self.get(path)

        except ValueError:
            return False

        return True

    def isdir(self, path: str):
        """Check if the specified path is a directory.

        Works like os.path.isdir
        """
        match = Match(path)
        return self.exists(path) and match.is_dir()

    def truncate(self):
        for bucket in self.listdir(ROOT):
            self.rmdir(join(ROOT, bucket), recursive=True)

    @_validate_directory
    def rmdir(self, path: str, recursive: bool = False):
        """Remove specified directory.

        If recursive flag is used, remove all content recursively
        like linux's rm -r.

        Args:
            path: path of a directory.
            recursive: remove content recursively.
        """
        match = Match(path)

        if match.is_root():
            if recursive:
                return self.truncate()
            raise DirectoryNotEmptyError("can not recursively delete "
                                         "unempty directory")

        dirs_to_delete = [path]

        while len(dirs_to_delete):
            current_dir_match = Match(dirs_to_delete.pop(0))
            objects_in_directory = self._get_objects_at(current_dir_match)
            files = []
            dirs = []

            if len(objects_in_directory) > 0:
                if not recursive:
                    raise DirectoryNotEmptyError("can not recursively delete "
                                                 "unempty directory")

                for obj in objects_in_directory:
                    if obj.is_dir:
                        dirs.append(
                            f"/{obj.bucket_name}/{obj.object_name}")
                    else:
                        files.append(obj.object_name)
                if len(files) > 0:
                    # list activates remove
                    list(self.minio_obj.remove_objects(match.bucket, files))

                dirs_to_delete += dirs

            if len(dirs) == 0 and not current_dir_match.is_bucket():
                self.minio_obj.remove_object(current_dir_match.bucket,
                                             current_dir_match.prefix)

        if match.is_bucket():
            try:
                self.minio_obj.remove_bucket(match.bucket)

            except BucketNotEmpty:
                raise DirectoryNotEmptyError("can not recursively delete "
                                             "unempty directory")

    def rm(self, path: str, recursive: bool = False):
        """Remove specified directory or file.

        If recursive flag is used, remove all content recursively.
        Works like linux's rm (-r).

        Args:
            path: path of a directory or a file.
            recursive: remove content recursively.
        """
        if self.isdir(path):
            return self.rmdir(path, recursive=recursive)

        match = Match(path)
        self.minio_obj.remove_object(match.bucket, match.relative_path)

    def _get_destination(self, from_path: str, to_path: str):
        from_match = Match(from_path)
        to_match = Match(to_path)

        if from_match.is_file():
            return Match.infer_operation_destination(from_match, to_match)

        if to_match.is_dir():
            if self.exists(to_match.path):
                return Match(join(to_match.path, basename(
                    join(from_match.bucket, from_match.prefix)[:-1]), ''))

        else:
            raise ValueError(
                "can not activate this method from directory to a file.")

        return to_match

    @_validate_directory
    def copy_recursively(self, from_path: str, to_path: str):
        """Copy recursively the content of from_path, to to_path.

        If you acctually wanted to copy from_path as a folder,
        add that folder's name to to_path and that new path
        will be created for you.

        Args:
            from_path: source path to a file.
            to_path: destination path.
            recursive: copy content recursively.
        """
        files_to_copy = []
        dirs_to_copy = [from_path]

        while len(dirs_to_copy) > 0:
            current_dir_match = Match(dirs_to_copy.pop(0))
            objects_in_directory = self._get_objects_at(current_dir_match)
            dirs = []

            for obj in objects_in_directory:
                obj_path = f"/{obj.bucket_name}/{obj.object_name}"
                if obj.is_dir:
                    dirs.append(obj_path)
                else:
                    files_to_copy.append(AttrDict(
                        from_path=obj_path,
                        to_path=join(to_path, obj_path.replace(from_path, '')),
                    ))

            if len(dirs) == 0:
                self.mkdirs(join(
                    to_path, current_dir_match.path.replace(from_path, '')))

            dirs_to_copy += dirs

        for obj_to_copy in files_to_copy:
            self.cp(
                from_path=obj_to_copy.from_path,
                to_path=obj_to_copy.to_path
            )

    def cp(self, from_path: str, to_path: str, recursive: bool = False):
        """Copy files from one directory to another.

        If to_path will be a path to a dictionary, the name will be
        the copied file name. if it will be a path with a file name,
        the name of the file will be this file's name.

        Works like linux's cp (-r).

        Args:
            from_path: source path to a file.
            to_path: destination path.
            recursive: copy content recursively.
        """
        from_match = Match(from_path)
        to_match = self._get_destination(from_path, to_path)

        if from_match.is_dir():
            if recursive:
                self.copy_recursively(from_match.path, to_match.path)
                return

            else:
                raise ValueError(
                    "copying a directory must be done recursively")

        self.minio_obj.copy_object(to_match.bucket, to_match.relative_path,
                                   join(from_match.bucket,
                                        from_match.relative_path))

    def mv(self, from_path: str, to_path: str, recursive: bool = False):
        """Move files from one directory to another.

        Works like linux's mv.

        Args:
            from_path: source path.
            to_path: destination path.
        """
        to_match = self._get_destination(from_path, to_path)
        try:
            self.cp(from_path, to_path, recursive)

        finally:
            if(self.exists(from_path) and self.exists(to_match.path)):
                self.rm(from_path, recursive)

    def get(self, path: str) -> ObjectData:
        """Get file or directory from minio.

        Args:
            path: path of a directory or a file.
        """
        match = Match(path)
        kwargs = AttrDict()

        if match.is_bucket():
            raise ValueError('Minio bucket has no representable object.')
        try:
            if match.is_file():
                kwargs.data = self.minio_obj.get_object(
                    match.bucket, match.relative_path).data

                details = self.minio_obj.stat_object(
                    match.bucket, match.relative_path)
                name = match.filename
                return_obj = File

            else:
                parent_directory = \
                    join(dirname(normpath(match.prefix)), '')
                objects = self.minio_obj.list_objects(
                    bucket_name=match.bucket,
                    prefix=parent_directory
                )

                details = next(filter(
                    lambda obj: obj.object_name == match.relative_path,
                    objects))
                name = join(basename(normpath(details.object_name)), '')
                return_obj = Folder

        except (NoSuchKey, StopIteration):
            raise ValueError(f"cannot access {path}: "
                             "No such file or directory")

        details_metadata = \
            self._extract_metadata(details.metadata)

        metadata = {
            "is_dir": details.is_dir,
            "last_modified": details.last_modified,
            "size": details.size
        }
        metadata.update(details_metadata)

        return return_obj(name=name, full_path=path,
                          metadata=AttrDict(metadata), **kwargs)

    def put_data(self, path: str, data: bytes,
                 metadata: Dict = None):
        """Put data in file inside a minio folder.

        Args:
            path: destination of the new file with its name in minio.
            data: the data that the file will contain in bytes.
            metadata: metadata dictionary to append the file.
        """
        match = Match(path)
        data_file = BytesIO(data)

        self.minio_obj.put_object(
            bucket_name=match.bucket,
            object_name=match.relative_path,
            data=data_file,
            length=len(data),
            metadata=metadata
        )
        data_file.close()

    def put_file(self, path: str, file_path: str, metadata: Dict = None):
        """Put file inside a minio folder.

        If file_path will be a path to a dictionary, the name will be
        the copied file name. if it will be a path with a file name,
        the name of the file will be this file's name.

        Args:
            path: destination of the new file in minio.
            file_path: the path to the file.
            metadata: metadata dictionary to append the file.
        """
        match = Match(path)

        if match.is_dir():
            match = Match(join(path, basename(file_path)))

        self.minio_obj.fput_object(match.bucket, match.relative_path,
                                   file_path, metadata=metadata)

    @_validate_directory
    def get_last_object(self, path: str) -> File:
        """Return the last modified object.

        Args:
            path: path of a directory.
        """
        match = Match(path)
        objects_names_in_dir = self.listdir(path, only_files=True)
        if len(objects_names_in_dir) == 0:
            return None

        last_object_name = objects_names_in_dir[0]
        relative_path = join(match.prefix, last_object_name)
        new_path = join(ROOT, match.bucket, relative_path)

        return self.get(new_path)
