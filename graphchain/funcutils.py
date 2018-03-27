"""
Utility functions employed by the graphchain module.
"""
import os
import pickle
import json
import logging
from collections import Iterable
import lz4.frame
from joblib import hash as joblib_hash
from joblib.func_inspect import get_func_code as joblib_getsource
import fs
import fs.osfs
import fs_s3fs
from errors import (InvalidPersistencyOption,
                    HashchainCompressionMismatch)


def get_storage(cachedir, persistency, s3bucket="graphchain-test-bucket"):
    """
    A storage handler that returns a `fs`-like storage object representing
    the persistency layer of the `hash-chain` cache files. The returned
    object has to be open across the lifetime of the graph optimization.
    """
    assert isinstance(cachedir, str) and isinstance(persistency, str)

    if persistency == "local":
        if not os.path.isdir(cachedir):
            os.makedirs(cachedir, exist_ok=True)
        storage = fs.osfs.OSFS(os.path.abspath(cachedir))
        return storage
    elif persistency == "s3":
        try:
            _storage = fs_s3fs.S3FS(s3bucket)
            if not _storage.isdir(cachedir):
                _storage.makedirs(cachedir, recreate=True)
            _storage.close()
            storage = fs_s3fs.S3FS(s3bucket, cachedir)
            return storage
        except Exception:
            # Something went wrong (probably) in the S3 access
            logging.error("Error encountered in S3 access "
                          f"(bucket='{s3bucket}')")
            raise
    else:
        logging.error(f"Unrecognized persistency option {persistency}")
        raise InvalidPersistencyOption


def load_hashchain(storage, compression=False):
    """
    Loads the `hash-chain` file found in the root directory of
    the `storage` filesystem object.
    """
    filename = "hashchain.json"  # constant
    if not storage.isfile(filename):
        logging.info(f"Creating a new hash-chain file {filename}")
        obj = dict()
        write_hashchain(obj, storage, compression=compression)
    else:
        with storage.open(filename, "r") as fid:
            hashchaindata = json.loads(fid.read())
        compr_option_lz4 = hashchaindata["compression"] == "lz4"
        obj = hashchaindata["hashchain"]
        if compr_option_lz4 ^ compression:
            raise HashchainCompressionMismatch(
                f"Compression option mismatch: "
                f"file={compr_option_lz4}, "
                f"optimizer={compression}.")
    return obj


def write_hashchain(obj, storage, version=1, compression=False):
    """
    Writes a `hash-chain` contained in ``obj`` to a file
    indicated by ``filename``.
    """
    filename = "hashchain.json"  # constant
    hashchaindata = {"version": str(version),
                     "compression": "lz4" if compression else "none",
                     "hashchain": obj}

    with storage.open(filename, "w") as fid:
        fid.write(json.dumps(hashchaindata, indent=4))


def wrap_to_store(obj, storage, objhash, compression=False, skipcache=False):
    """
    Wraps a callable object in order to execute it and store its result.
    """
    def exec_store_wrapper(*args, **kwargs):
        """
        Simple execute and store wrapper.
        """
        _cachedir = "__cache__"
        if not storage.isdir(_cachedir):
            storage.makedirs(_cachedir, recreate=True)

        if callable(obj):
            ret = obj(*args, **kwargs)
            objname = obj.__name__
        else:
            ret = obj
            objname = "constant=" + str(obj)

        if compression and not skipcache:
            logging.info(f"* [{objname}] EXEC-STORE-COMPRESS (hash={objhash})")
        elif not compression and not skipcache:
            logging.info(f"* [{objname}] EXEC-STORE (hash={objhash})")
        else:
            logging.info(f"* [{objname}] EXEC *ONLY* (hash={objhash})")

        if not skipcache:
            data = pickle.dumps(ret)
            if compression:
                filepath = fs.path.join(_cachedir, objhash + ".pickle.lz4")
                data = lz4.frame.compress(data)
            else:
                filepath = fs.path.join(_cachedir, objhash + ".pickle")

            with storage.open(filepath, "wb") as fid:
                fid.write(data)

        return ret

    return exec_store_wrapper


def wrap_to_load(obj, storage, objhash, compression=False):
    """
    Wraps a callable object in order not to execute it and rather
    load its result.
    """
    def loading_wrapper():  # no arguments needed
        """
        Simple load wrapper.
        """
        _cachedir = "__cache__"
        assert storage.isdir(_cachedir)

        if compression:
            filepath = fs.path.join(_cachedir, objhash + ".pickle.lz4")
        else:
            filepath = fs.path.join(_cachedir, objhash + ".pickle")
        assert storage.isfile(filepath)

        if callable(obj):
            objname = obj.__name__
        else:
            objname = "constant=" + str(obj)

        if compression:
            logging.info(f"* [{objname}] LOAD-UNCOMPRESS (hash={objhash})")
        else:
            logging.info(f"* [{objname}] LOAD (hash={objhash})")

        if compression:
            with storage.open(filepath, "rb") as _fid:
                with lz4.frame.open(_fid, mode="r") as fid:
                    ret = pickle.loads(fid.read())
        else:
            with storage.open(filepath, "rb") as fid:
                ret = pickle.load(fid)
        return ret

    return loading_wrapper


def get_hash(task, keyhashmap=None):
    """
    Calculates and returns the hash corresponding to a dask task
    ``task`` using the hashes of its dependencies, input arguments
    and source code of the function associated to the task. Any
    available hashes are passed in ``keyhashmap``.
    """
    assert task is not None
    fnhash_list = []
    arghash_list = []
    dephash_list = []

    if isinstance(task, Iterable):
        # An iterable (tuple) would correspond to a delayed function
        for taskelem in task:
            if callable(taskelem):
                # function
                sourcecode = joblib_getsource(taskelem)[0]
                fnhash_list.append(joblib_hash(sourcecode))
            else:
                if (isinstance(keyhashmap, dict) and
                        taskelem in keyhashmap.keys()):
                    # we have a dask graph key
                    dephash_list.append(keyhashmap[taskelem])
                else:
                    arghash_list.append(joblib_hash(taskelem))
    else:
        # A non iterable i.e. constant
        arghash_list.append(joblib_hash(task))

    # Account for the fact that dependencies are also arguments
    arghash_list.append(joblib_hash(joblib_hash(len(dephash_list))))

    # Calculate subhashes
    src_hash = joblib_hash("".join(fnhash_list))
    arg_hash = joblib_hash("".join(arghash_list))
    dep_hash = joblib_hash("".join(dephash_list))

    subhashes = {"src": src_hash, "arg": arg_hash, "dep": dep_hash}
    objhash = joblib_hash(src_hash + arg_hash + dep_hash)
    return objhash, subhashes


def analyze_hash_miss(hashchain, htask, hcomp, taskname):
    """
    Function that analyzes and gives out a printout of
    possible hass miss reasons. The importance of a
    candidate is calculated as Ic = Nm/Nc where:
        - Ic is an imporance coefficient;
        - Nm is the number of subhashes matched;
        - Nc is the number that candidate code
        appears.
    For example, if there are 1 candidates with
    a code 2 (i.e. arguments hash match) and
    10 candidates with code 6 (i.e. code and
    arguments match), the more important candidate
    is the one with a sing
    """
    from collections import defaultdict
    codecm = defaultdict(int)              # codes count map
    for key in hashchain.keys():
        hashmatches = (hashchain[key]["src"] == hcomp["src"],
                       hashchain[key]["arg"] == hcomp["arg"],
                       hashchain[key]["dep"] == hcomp["dep"])
        codecm[hashmatches] += 1

    dists = {k: sum(k)/codecm[k] for k in codecm.keys()}
    sdists = sorted(list(dists.items()), key=lambda x: x[1], reverse=True)

    def ok_or_missing(arg):
        """
        Function that returns 'OK' if the input
        argument is True and 'MISSING' otherwise.
        """
        if arg is True:
            out = "OK"
        elif arg is False:
            out = "MISS"
        else:
            out = "ERROR"
        return out

    logging.info(f"ID:{taskname}, HASH:{htask}")
    msgstr = "  `- src={:>4}, arg={:>4} dep={:>4} has {} candidates."
    for value in sdists:
        code, _ = value
        logging.info(msgstr.format(ok_or_missing(code[0]),
                                   ok_or_missing(code[1]),
                                   ok_or_missing(code[2]),
                                   codecm[code]))
