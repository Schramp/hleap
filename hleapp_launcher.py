"""Run ALEAPP with its files fetched on demand from Hansken (issue #15).

Started by the plugin in the ALEAPP venv, with the ALEAPP checkout as working directory and the ALEAPP command line
as arguments ('-t fs -i <empty dir> ...'). It replaces ALEAPP's FileSeekerDir by HanskenSeeker for this run only
(aleapp.py does 'from scripts.search_files import *', so the name is looked up in the aleapp module) and calls
aleapp.main(). The ALEAPP checkout itself is not changed.

HanskenSeeker.search(glob) asks the plugin over the RPC channel (hleapp_rpc) to find the matching Hansken traces
and write them into ALEAPP's data folder; it returns the staged paths like FileSeekerDir does.
"""
import os
import sys

sys.path.insert(0, os.getcwd())  # the ALEAPP checkout

import aleapp  # noqa: E402
from scripts.search_files import FileInfo, FileSeekerBase  # noqa: E402

from hleapp_rpc import RpcClient  # noqa: E402


class HanskenSeeker(FileSeekerBase):
    """Drop-in for FileSeekerDir(directory, data_folder); directory is an empty placeholder."""

    def __init__(self, directory, data_folder, client=None):
        FileSeekerBase.__init__(self)
        self.directory = directory
        self.data_folder = data_folder
        self.searched = {}
        self.copied = {}
        self.file_infos = {}
        self._client = client or RpcClient.from_env()

    def search(self, filepattern, return_on_first_hit=False, force=False):
        if filepattern in self.searched and not force:
            pathlist = self.searched[filepattern]
            return pathlist[0] if return_on_first_hit and pathlist else pathlist
        staged = self._client.call('search_and_stage', glob=filepattern, data_folder=self.data_folder,
                                   first_hit=return_on_first_hit)
        pathlist = []
        for item in staged:
            self.copied[item['source_path']] = item['staged']
            self.file_infos[item['staged']] = FileInfo(item['source_path'], item['ctime'], item['mtime'])
            pathlist.append(item['staged'])
        self.searched[filepattern] = pathlist
        return pathlist[0] if return_on_first_hit and pathlist else pathlist

    def cleanup(self):
        self._client.close()


def main():
    aleapp.FileSeekerDir = HanskenSeeker
    sys.argv = ['aleapp.py'] + sys.argv[1:]
    aleapp.main()


if __name__ == '__main__':
    main()
