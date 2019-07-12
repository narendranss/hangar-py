import hashlib
import io
import os
import tempfile
import threading
import time
from concurrent import futures
from os.path import join as pjoin

import blosc
import grpc
import lmdb
import msgpack
import numpy as np

from . import config
from . import chunks
from . import hangar_service_pb2
from . import hangar_service_pb2_grpc
from . import request_header_validator_interceptor
from .content import ContentWriter
from .. import constants as c
from ..context import Environments, TxnRegister
from ..backends.selection import BACKEND_ACCESSOR_MAP, backend_decoder
from ..records import commiting
from ..records import hashs
from ..records import heads
from ..records import parsing
from ..records import queries
from ..records import summarize
from ..utils import set_blosc_nthreads

set_blosc_nthreads()


class HangarServer(hangar_service_pb2_grpc.HangarServiceServicer):

    def __init__(self, repo_path, overwrite=False):

        self.env = Environments(repo_path=repo_path)
        try:
            self.env._init_repo(
                user_name='SERVER_USER',
                user_email='SERVER_USER@HANGAR.SERVER',
                remove_old=overwrite)
        except OSError:
            pass

        self._rFs = {}
        for backend, accessor in BACKEND_ACCESSOR_MAP.items():
            if accessor is not None:
                self._rFs[backend] = accessor(
                    repo_path=self.env.repo_path,
                    schema_shape=None,
                    schema_dtype=None)
                self._rFs[backend].open('r')

        src_path = pjoin(os.path.dirname(__file__), c.CONFIG_SERVER_NAME)
        config.ensure_file(src_path, destination=repo_path, comment=False)
        config.refresh(paths=[repo_path])

        self.txnregister = TxnRegister()
        self.repo_path = self.env.repo_path
        self.data_dir = pjoin(self.repo_path, c.DIR_DATA)
        self.CW = ContentWriter(self.env)

    # -------------------- Client Config --------------------------------------

    def PING(self, request, context):
        '''Test function. PING -> PONG!
        '''
        reply = hangar_service_pb2.PingReply(result='PONG')
        return reply

    def GetClientConfig(self, request, context):
        '''Return parameters to the client to set up channel options as desired by the server.
        '''
        push_max_nbytes = str(config.get('client.grpc.push_max_nbytes'))
        enable_compression = config.get('client.grpc.enable_compression')
        enable_compression = str(1) if enable_compression is True else str(0)
        optimization_target = config.get('client.grpc.optimization_target')

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.GetClientConfigReply(error=err)
        reply.config['push_max_nbytes'] = push_max_nbytes
        reply.config['enable_compression'] = enable_compression
        reply.config['optimization_target'] = optimization_target
        return reply

    # -------------------- Branch Record --------------------------------------

    def FetchBranchRecord(self, request, context):
        '''Return the current HEAD commit of a particular branch
        '''
        branch_name = request.rec.name
        try:
            head = heads.get_branch_head_commit(self.env.branchenv, branch_name)
            rec = hangar_service_pb2.BranchRecord(name=branch_name, commit=head)
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            reply = hangar_service_pb2.FetchBranchRecordReply(rec=rec, error=err)
        except ValueError:
            msg = f'BRANCH: {branch_name} DOES NOT EXIST ON SERVER.'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            reply = hangar_service_pb2.FetchBranchRecordReply(error=err)
        return reply

    def PushBranchRecord(self, request, context):
        '''Update the HEAD commit of a branch, creating the record if not previously existing.
        '''
        branch_name = request.rec.name
        commit = request.rec.commit
        branch_names = heads.get_branch_names(self.env.branchenv)
        if branch_name not in branch_names:
            heads.create_branch(self.env.branchenv, branch_name=branch_name, base_commit=commit)
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        else:
            current_head = heads.get_branch_head_commit(self.env.branchenv, branch_name)
            if current_head == commit:
                msg = f'NO CHANGE TO BRANCH: {branch_name} WITH HEAD: {current_head}'
                context.set_details(msg)
                context.set_code(grpc.StatusCode.ALREADY_EXISTS)
                err = hangar_service_pb2.ErrorProto(code=6, message=msg)
            else:
                heads.set_branch_head_commit(self.env.branchenv, branch_name, commit)
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')

        reply = hangar_service_pb2.PushBranchRecordReply(error=err)
        return reply

    # -------------------------- Commit Record --------------------------------

    def FetchCommit(self, request, context):
        '''Return raw data representing contents, spec, and parents of a commit hash.
        '''
        commit = request.commit
        commitRefKey = parsing.commit_ref_db_key_from_raw_key(commit)
        commitParentKey = parsing.commit_parent_db_key_from_raw_key(commit)
        commitSpecKey = parsing.commit_spec_db_key_from_raw_key(commit)

        reftxn = self.txnregister.begin_reader_txn(self.env.refenv)
        try:
            commitRefVal = reftxn.get(commitRefKey, default=False)
            commitParentVal = reftxn.get(commitParentKey, default=False)
            commitSpecVal = reftxn.get(commitSpecKey, default=False)
        finally:
            self.txnregister.abort_reader_txn(self.env.refenv)

        if commitRefVal is False:
            msg = f'COMMIT: {commit} DOES NOT EXIST ON SERVER'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            reply = hangar_service_pb2.FetchCommitReply(commit=commit, error=err)
            yield reply
            raise StopIteration()
        else:
            raw_data_chunks = chunks.chunk_bytes(commitRefVal)
            bsize = len(commitRefVal)
            commit_proto = hangar_service_pb2.CommitRecord()
            commit_proto.parent = commitParentVal
            commit_proto.spec = commitSpecVal
            reply = hangar_service_pb2.FetchCommitReply(commit=commit, total_byte_size=bsize)
            for chunk in raw_data_chunks:
                commit_proto.ref = chunk
                reply.record.CopyFrom(commit_proto)
                yield reply

    def PushCommit(self, request_iterator, context):
        '''Record the contents of a new commit sent to the server.

        Will not overwrite data if a commit hash is already recorded on the server.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                refBytes, offset = bytearray(request.total_byte_size), 0
                specVal = request.record.spec
                parentVal = request.record.parent
            size = len(request.record.ref)
            refBytes[offset: offset + size] = request.record.ref
            offset += size

        digest = self.CW.commit(commit, parentVal, specVal, refBytes)
        if not digest:
            msg = f'COMMIT: {commit} ALREADY EXISTS'
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(msg)
            err = hangar_service_pb2.ErrorProto(code=6, message=msg)
        else:
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            commiting.move_process_data_to_store(self.env.repo_path, remote_operation=True)

        reply = hangar_service_pb2.PushCommitReply(error=err)
        return reply

    # --------------------- Schema Record -------------------------------------

    def FetchSchema(self, request, context):
        '''Return the raw byte specification of a particular schema with requested hash.
        '''
        schema_hash = request.rec.digest
        schemaKey = parsing.hash_schema_db_key_from_raw_key(schema_hash)
        hashTxn = self.txnregister.begin_reader_txn(self.env.hashenv)
        try:
            schemaExists = hashTxn.get(schemaKey, default=False)
            if schemaExists is not False:
                print(f'found schema: {schema_hash}')
                rec = hangar_service_pb2.SchemaRecord(digest=schema_hash, blob=schemaExists)
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')
            else:
                print(f'not exists: {schema_hash}')
                msg = f'SCHEMA HASH: {schema_hash} DOES NOT EXIST ON SERVER'
                context.set_details(msg)
                context.set_code(grpc.StatusCode.NOT_FOUND)
                err = hangar_service_pb2.ErrorProto(code=5, message=msg)
                rec = hangar_service_pb2.SchemaRecord(digest=schema_hash)
        finally:
            self.txnregister.abort_reader_txn(self.env.hashenv)

        reply = hangar_service_pb2.FetchSchemaReply(rec=rec, error=err)
        return reply

    def PushSchema(self, request, context):
        '''Add a new schema byte specification record.

        Will not overwrite a schema hash which already exists on the server.
        '''
        schema_hash = request.rec.digest
        schema_val = request.rec.blob

        digest = self.CW.schema(schema_hash, schema_val)
        if not digest:
            print(f'exists: {schema_val}')
            msg = f'SCHEMA: {schema_hash} ALREADY EXISTS ON SERVER'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            err = hangar_service_pb2.ErrorProto(code=6, message=msg)
        else:
            print(f'created new: {schema_val}')
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushSchemaReply(error=err)
        return reply

    # ---------------------------- Data ---------------------------------------

    def FetchData(self, request_iterator, context):
        '''Return a packed byte representation of samples corresponding to a digest.

        Please see comments below which explain why not all requests are
        guarrenteed to fully complete in one operation.
        '''

        for idx, request in enumerate(request_iterator):
            if idx == 0:
                uncomp_nbytes = request.uncomp_nbytes
                comp_nbytes = request.comp_nbytes
                dBytes, offset = bytearray(comp_nbytes), 0
            size = len(request.raw_data)
            dBytes[offset: offset + size] = request.raw_data
            offset += size

        uncompBytes = blosc.decompress(dBytes)
        if uncomp_nbytes != len(uncompBytes):
            msg = f'Expected nbytes data sent: {uncomp_nbytes} != recieved {comp_nbytes}'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.DATA_LOSS)
            err = hangar_service_pb2.ErrorProto(code=15, message=msg)
            reply = hangar_service_pb2.FetchDataReply(error=err)
            yield reply
            raise StopIteration()

        buff = io.BytesIO(uncompBytes)
        unpacker = msgpack.Unpacker(
            buff, use_list=False, raw=False, max_buffer_size=1_000_000_000)

        # We recieve a list of digests to send to the client. One consideration
        # we have is that there is no way to know how much memory will be used
        # when the data is read from disk. Samples are compressed against
        # eachother before going over the wire, which means its preferable to
        # read in as much as possible. However, since we don't want to overload
        # the client system when the binary blob is decompressed into individual
        # tensors, we set some maximum size which tensors can occupy when
        # uncompressed. When we recieve a list of digests whose data size is in
        # excess of this limit, we just say sorry to the client, send the chunk
        # of digests/tensors off to them as is (incomplete), and request that
        # the client figure out what it still needs and ask us again.

        totalSize = 0
        buf = io.BytesIO()
        packer = msgpack.Packer(use_bin_type=True)
        hashTxn = self.txnregister.begin_reader_txn(self.env.hashenv)
        fetch_max_nbytes = config.get('server.grpc.fetch_max_nbytes')
        try:
            for digest in unpacker:
                hashKey = parsing.hash_data_db_key_from_raw_key(digest)
                hashVal = hashTxn.get(hashKey, default=False)
                if hashVal is False:
                    msg = f'HASH DOES NOT EXIST: {hashKey}'
                    context.set_details(msg)
                    context.set_code(grpc.StatusCode.NOT_FOUND)
                    err = hangar_service_pb2.ErrorProto(code=5, message=msg)
                    reply = hangar_service_pb2.FetchDataReply(error=err)
                    yield reply
                    raise StopIteration()
                else:
                    spec = backend_decoder(hashVal)
                    tensor = self._rFs[spec.backend].read_data(spec)

                p = packer.pack((digest, tensor.shape, tensor.dtype.num, tensor.tobytes()))
                buf.seek(totalSize)
                buf.write(p)
                totalSize += len(p)

                if totalSize >= fetch_max_nbytes:
                    err = hangar_service_pb2.ErrorProto(code=0, message='OK')
                    cIter = chunks.tensorChunkedIterator(
                        buf=buf, uncomp_nbytes=totalSize, itemsize=tensor.itemsize,
                        pb2_request=hangar_service_pb2.FetchDataReply, err=err)
                    yield from cIter
                    time.sleep(0.1)
                    msg = 'HANGAR REQUESTED RETRY: developer enforced limit on returned '\
                          'raw data size to prevent memory overload of user system.'
                    context.set_details(msg)
                    context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                    err = hangar_service_pb2.ErrorProto(code=8, message=msg)
                    yield hangar_service_pb2.FetchDataReply(error=err, raw_data=b'')
                    raise StopIteration()

        except StopIteration:
            totalSize = 0

        finally:
            # finish sending all remaining tensors if max size hash not been hit.
            if totalSize > 0:
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')
                cIter = chunks.tensorChunkedIterator(
                    buf=buf,
                    uncomp_nbytes=totalSize,
                    itemsize=tensor.itemsize,
                    pb2_request=hangar_service_pb2.FetchDataReply,
                    err=err)
                yield from cIter
            buf.close()
            self.txnregister.abort_reader_txn(self.env.hashenv)

    def PushData(self, request_iterator, context):
        '''Recieve compressed streams of binary data from the client.

        In order to prevent errors or malicious behavior, the cryptographic hash
        of every tensor is calculated and compared to what the client "said" it
        is. If an error is detected, no sample in the entire stream will be
        saved to disk.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                uncomp_nbytes = request.uncomp_nbytes
                comp_nbytes = request.comp_nbytes
                dBytes, offset = bytearray(comp_nbytes), 0
            size = len(request.raw_data)
            dBytes[offset: offset + size] = request.raw_data
            offset += size

        uncompBytes = blosc.decompress(dBytes)
        if uncomp_nbytes != len(uncompBytes):
            msg = f'ERROR: uncomp_nbytes sent: {uncomp_nbytes} != recieved {comp_nbytes}'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.DATA_LOSS)
            err = hangar_service_pb2.ErrorProto(code=15, message=msg)
            reply = hangar_service_pb2.PushDataReply(error=err)
            return reply

        buff = io.BytesIO(uncompBytes)
        unpacker = msgpack.Unpacker(
            buff, use_list=False, raw=False, max_buffer_size=1_000_000_000)
        # hashTxn = self.txnregister.begin_writer_txn(self.env.hashenv)
        recieved_data = []
        for data in unpacker:
            digest, schema_hash, dShape, dTypeN, dBytes = data
            tensor = np.frombuffer(dBytes, dtype=np.typeDict[dTypeN]).reshape(dShape)
            recieved_hash = hashlib.blake2b(tensor.tobytes(), digest_size=20).hexdigest()
            if recieved_hash != digest:
                msg = f'HASH MANGLED, recieved: {recieved_hash} != expected digest: {digest}'
                context.set_details(msg)
                context.set_code(grpc.StatusCode.DATA_LOSS)
                err = hangar_service_pb2.ErrorProto(code=15, message=msg)
                reply = hangar_service_pb2.PushDataReply(error=err)
                return reply
            recieved_data.append((recieved_hash, tensor))
        saved_digests = self.CW.data(schema_hash, recieved_data)
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushDataReply(error=err)
        return reply

    # ----------------------------- Label Data --------------------------------

    def FetchLabel(self, request, context):
        '''Retrieve the metadata value corresponding to some particular hash digests
        '''
        digest = request.rec.digest
        digest_type = request.rec.type
        rec = hangar_service_pb2.HashRecord(digest=digest, type=digest_type)
        reply = hangar_service_pb2.FetchLabelReply(rec=rec)

        labelKey = parsing.hash_meta_db_key_from_raw_key(digest)
        labelTxn = self.txnregister.begin_reader_txn(self.env.labelenv)
        try:
            labelVal = labelTxn.get(labelKey, default=False)
            if labelVal is False:
                msg = f'DOES NOT EXIST: labelval with key: {labelKey}'
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(msg)
                err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            else:
                err = hangar_service_pb2.ErrorProto(code=0, message='OK')
                compLabelVal = blosc.compress(labelVal)
                reply.blob = compLabelVal
        finally:
            self.txnregister.abort_reader_txn(self.env.labelenv)

        reply.error.CopyFrom(err)
        return reply

    def PushLabel(self, request, context):
        '''Add a metadata key/value pair to the server with a particular digest.

        Like data tensors, the cryptographic hash of each value is verified
        before the data is actually placed on the server file system.
        '''
        req_digest = request.rec.digest

        uncompBlob = blosc.decompress(request.blob)
        recieved_hash = hashlib.blake2b(uncompBlob, digest_size=20).hexdigest()
        if recieved_hash != req_digest:
            msg = f'HASH MANGED: recieved_hash: {recieved_hash} != digest: {req_digest}'
            context.set_details(msg)
            context.set_code(grpc.StatusCode.DATA_LOSS)
            err = hangar_service_pb2.ErrorProto(code=15, message=msg)
            reply = hangar_service_pb2.PushLabelReply(error=err)
            return reply

        digest = self.CW.label(recieved_hash, uncompBlob)
        if not digest:
            msg = f'HASH ALREADY EXISTS: {req_digest}'
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(msg)
            err = hangar_service_pb2.ErrorProto(code=6, message=msg)
        else:
            err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.PushLabelReply(error=err)
        return reply

    # ------------------------ Fetch Find Missing -----------------------------------

    def FetchFindMissingCommits(self, request, context):
        '''Determine commit digests existing on the server which are not present on the client.
        '''
        c_branch_name = request.branch.name
        c_ordered_commits = request.commits

        try:
            s_history = summarize.list_history(
                refenv=self.env.refenv,
                branchenv=self.env.branchenv,
                branch_name=c_branch_name)
        except ValueError:
            msg = f'BRANCH NOT EXIST. Name: {c_branch_name}'
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(msg)
            err = hangar_service_pb2.ErrorProto(code=5, message=msg)
            reply = hangar_service_pb2.FindMissingCommitsReply(error=err)
            return reply

        s_orderset = set(s_history['order'])
        c_orderset = set(c_ordered_commits)
        c_missing = list(s_orderset.difference(c_orderset))   # only difference to PushFindMissingCommits

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        if len(c_missing) == 0:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=s_history['head'])
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
        else:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=s_history['head'])
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
            reply.commits.extend(c_missing)

        return reply

    def PushFindMissingCommits(self, request, context):
        '''Determine commit digests existing on the client which are not present on the server.
        '''
        c_branch_name = request.branch.name
        c_head_commit = request.branch.commit
        c_ordered_commits = request.commits

        s_commits = commiting.list_all_commits(self.env.refenv)
        s_orderset = set(s_commits)
        c_orderset = set(c_ordered_commits)
        s_missing = list(c_orderset.difference(s_orderset))  # only difference to FetchFindMissingCommits

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        if len(s_missing) == 0:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=c_head_commit)
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
        else:
            brch = hangar_service_pb2.BranchRecord(name=c_branch_name, commit=c_head_commit)
            reply = hangar_service_pb2.FindMissingCommitsReply(branch=brch, error=err)
            reply.commits.extend(s_missing)

        return reply

    def FetchFindMissingHashRecords(self, request_iterator, context):
        '''Determine data tensor hash records existing on the server and not on the client.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size

        uncompBytes = blosc.decompress(hBytes)
        c_hashset = set(msgpack.unpackb(uncompBytes, raw=False, use_list=False))

        with tempfile.TemporaryDirectory() as tempD:
            tmpDF = os.path.join(tempD, 'test.lmdb')
            tmpDB = lmdb.open(path=tmpDF, **c.LMDB_SETTINGS)
            commiting.unpack_commit_ref(self.env.refenv, tmpDB, commit)
            s_hashes_schemas = queries.RecordQuery(tmpDB).data_hash_to_schema_hash()
            s_hashes = set(s_hashes_schemas.keys())
            tmpDB.close()

        c_missing = list(s_hashes.difference(c_hashset))
        c_hash_schemas = [(c_mis, s_hashes_schemas[c_mis]) for c_mis in c_missing]
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingHashRecordsReply
        cIter = chunks.missingHashIterator(commit, c_hash_schemas, err, response_pb)
        yield from cIter

    def PushFindMissingHashRecords(self, request_iterator, context):
        '''Determine data tensor hash records existing on the client and not on the server.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size

        uncompBytes = blosc.decompress(hBytes)
        c_hashset = set(msgpack.unpackb(uncompBytes, raw=False, use_list=False))
        s_hashset = set(hashs.HashQuery(self.env.hashenv).list_all_hash_keys_raw())
        s_missing = list(c_hashset.difference(s_hashset))
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingHashRecordsReply
        cIter = chunks.missingHashIterator(commit, s_missing, err, response_pb)
        yield from cIter

    def FetchFindMissingLabels(self, request_iterator, context):
        '''Determine metadata hash digest records existing on the server and not on the client.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size
        uncompBytes = blosc.decompress(hBytes)
        c_hashset = set(msgpack.unpackb(uncompBytes, raw=False, use_list=False))

        with tempfile.TemporaryDirectory() as tempD:
            tmpDF = os.path.join(tempD, 'test.lmdb')
            tmpDB = lmdb.open(path=tmpDF, **c.LMDB_SETTINGS)
            commiting.unpack_commit_ref(self.env.refenv, tmpDB, commit)
            s_hashes = set(queries.RecordQuery(tmpDB).metadata_hashes())
            tmpDB.close()

        c_missing = list(s_hashes.difference(c_hashset))
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingLabelsReply
        cIter = chunks.missingHashIterator(commit, c_missing, err, response_pb)
        yield from cIter

    def PushFindMissingLabels(self, request_iterator, context):
        '''Determine metadata hash digest records existing on the client and not on the server.
        '''
        for idx, request in enumerate(request_iterator):
            if idx == 0:
                commit = request.commit
                hBytes, offset = bytearray(request.total_byte_size), 0
            size = len(request.hashs)
            hBytes[offset: offset + size] = request.hashs
            offset += size
        uncompBytes = blosc.decompress(hBytes)
        c_hashset = set(msgpack.unpackb(uncompBytes, raw=False, use_list=True))
        s_hash_keys = list(hashs.HashQuery(self.env.labelenv).list_all_hash_keys_db())
        s_hashes = map(parsing.hash_meta_raw_key_from_db_key, s_hash_keys)
        s_hashset = set(s_hashes)

        s_missing = list(c_hashset.difference(s_hashset))
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        response_pb = hangar_service_pb2.FindMissingLabelsReply
        cIter = chunks.missingHashIterator(commit, s_missing, err, response_pb)
        yield from cIter

    def FetchFindMissingSchemas(self, request, context):
        '''Determine schema hash digest records existing on the server and not on the client.
        '''
        commit = request.commit
        c_schemas = set(request.schema_digests)

        with tempfile.TemporaryDirectory() as tempD:
            tmpDF = os.path.join(tempD, 'test.lmdb')
            tmpDB = lmdb.open(path=tmpDF, **c.LMDB_SETTINGS)
            commiting.unpack_commit_ref(self.env.refenv, tmpDB, commit)
            s_schemas = set(queries.RecordQuery(tmpDB).schema_hashes())
            tmpDB.close()

        c_missing = list(s_schemas.difference(c_schemas))
        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.FindMissingSchemasReply(commit=commit, error=err)
        reply.schema_digests.extend(c_missing)
        return reply

    def PushFindMissingSchemas(self, request, context):
        '''Determine schema hash digest records existing on the client and not on the server.
        '''
        commit = request.commit
        c_schemas = set(request.schema_digests)
        s_schemas = set(hashs.HashQuery(self.env.hashenv).list_all_schema_keys_raw())
        s_missing = list(c_schemas.difference(s_schemas))

        err = hangar_service_pb2.ErrorProto(code=0, message='OK')
        reply = hangar_service_pb2.FindMissingSchemasReply(commit=commit, error=err)
        reply.schema_digests.extend(s_missing)
        return reply


def serve(hangar_path: os.PathLike,
          overwrite: bool = False,
          *,
          channel_address: str = None,
          restrict_push: bool = None,
          username: str = None,
          password: str = None) -> tuple:
    '''Start serving the GRPC server. Should only be called once.

    Raises:
        e: critical error from one of the workers.
    '''

    # ------------------- Configure Server ------------------------------------

    src_path = pjoin(os.path.dirname(__file__), 'config_server.yml')
    dest_path = pjoin(hangar_path, c.DIR_HANGAR_SERVER)
    config.ensure_file(src_path, destination=dest_path, comment=False)
    config.refresh(paths=[dest_path])

    enable_compression = config.get('server.grpc.enable_compression')
    optimization_target = config.get('server.grpc.optimization_target')
    if channel_address is None:
        channel_address = config.get('server.grpc.channel_address')
    max_thread_pool_workers = config.get('server.grpc.max_thread_pool_workers')
    max_concurrent_rpcs = config.get('server.grpc.max_concurrent_rpcs')

    if (restrict_push is None) and (username is None) and (password is None):
        admin_restrict_push = config.get('server.admin.restrict_push')
        admin_username = config.get('server.admin.username')
        admin_password = config.get('server.admin.password')
    else:
        admin_restrict_push = restrict_push
        admin_username = username
        admin_password = password
    msg = 'PERMISSION ERROR: PUSH OPERATIONS RESTRICTED FOR CALLER'
    code = grpc.StatusCode.PERMISSION_DENIED
    interc = request_header_validator_interceptor.RequestHeaderValidatorInterceptor(
        admin_restrict_push, admin_username, admin_password, code, msg)

    # ---------------- Start the thread pool for the grpc server --------------

    grpc_thread_pool = futures.ThreadPoolExecutor(
        max_workers=max_thread_pool_workers,
        thread_name_prefix='grpc_thread_pool')
    server = grpc.server(
        thread_pool=grpc_thread_pool,
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        options=[('grpc.default_compression_algorithm', enable_compression),
                 ('grpc.optimization_target', optimization_target)],
        interceptors=(interc,))

    # ------------------- Start the GRPC server -------------------------------

    hangserv = HangarServer(dest_path, overwrite)
    hangar_service_pb2_grpc.add_HangarServiceServicer_to_server(hangserv, server)
    server.add_insecure_port(channel_address)
    return (server, hangserv, channel_address)


if __name__ == '__main__':
    workdir = os.getcwd()
    print(workdir)
    serve(workdir)
