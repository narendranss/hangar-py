# parsing constants

SEP_KEY = ':'
SEP_LST = ' '
SEP_CMT = ' << '
SEP_SLC = "*"
SEP_HSH = '$'

CMT_KV_JOIN_KEY = SEP_LST.encode()
CMT_DIGEST_JOIN_KEY = ''
CMT_REC_JOIN_KEY = SEP_HSH.encode()

K_INT = f'#'
K_BRANCH = f'branch{SEP_KEY}'
K_HEAD = 'head'
K_REMOTES = f'remote{SEP_KEY}'
K_STGARR = f'a{SEP_KEY}'
K_STGMETA = f'l{SEP_KEY}'
K_SCHEMA = f's{SEP_KEY}'
K_HASH = f'h{SEP_KEY}'
K_WLOCK = f'writerlock{SEP_KEY}'
K_VERSION = 'software_version'

WLOCK_SENTINAL = 'LOCK_AVAILABLE'

# directory names

DIR_HANGAR = '.hangar'
DIR_HANGAR_SERVER = '.hangar_server'
DIR_DATA = 'data'
DIR_DATA_STORE = 'store_data'
DIR_DATA_STAGE = 'stage_data'
DIR_DATA_REMOTE = 'remote_data'

# configuration file names:

CONFIG_USER_NAME = 'config_user.yml'
CONFIG_SERVER_NAME = 'config_server.yml'

# LMDB database names and settings.

LMDB_SETTINGS = {
    'map_size': 2_000_000_000,
    'meminit': False,
    'subdir': False,
    'lock': False,
    'max_spare_txns': 2,
}

LMDB_REF_NAME = 'ref.lmdb'
LMDB_HASH_NAME = 'hash.lmdb'
LMDB_META_NAME = 'meta.lmdb'
LMDB_BRANCH_NAME = 'branch.lmdb'
LMDB_STAGE_REF_NAME = 'stage_ref.lmdb'
LMDB_STAGE_HASH_NAME = 'stage_hash.lmdb'


# readme file

README_FILE_NAME = 'README.txt'