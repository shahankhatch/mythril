"""This module contains a LevelDB client."""
import binascii
import rlp
from mythril.ethereum.interface.leveldb.accountindexing import CountableList
from mythril.ethereum.interface.leveldb.accountindexing import (
    ReceiptForStorage,
    AccountIndexer,
)
import logging
from ethereum import utils
from ethereum.block import BlockHeader, Block
from mythril.ethereum.interface.leveldb.state import State
from mythril.ethereum.interface.leveldb.eth_db import ETH_DB
from mythril.ethereum.evmcontract import EVMContract
from mythril.exceptions import AddressNotFoundError

log = logging.getLogger(__name__)

# Per https://github.com/ethereum/go-ethereum/blob/master/core/rawdb/schema.go
# prefixes and suffixes for keys in geth
header_prefix = b"h"  # header_prefix + num (uint64 big endian) + hash -> header
body_prefix = b"b"  # body_prefix + num (uint64 big endian) + hash -> block body
num_suffix = b"n"  # header_prefix + num (uint64 big endian) + num_suffix -> hash
block_hash_prefix = b"H"  # block_hash_prefix + hash -> num (uint64 big endian)
block_receipts_prefix = (
    b"r"  # block_receipts_prefix + num (uint64 big endian) + hash -> block receipts
)
# known geth keys
head_header_key = b"LastBlock"  # head (latest) header hash
# custom prefixes
address_prefix = b"AM"  # address_prefix + hash -> address
# custom keys
address_mapping_head_key = b"accountMapping"  # head (latest) number of indexed block


def _format_block_number(number):
    """Format block number to uint64 big endian."""
    return utils.zpad(utils.int_to_big_endian(number), 8)


def _encode_hex(v):
    """Encode a hash string as hex."""
    return "0x" + utils.encode_hex(v)


class LevelDBReader(object):
    """LevelDB reading interface, can be used with snapshot."""

    def __init__(self, db):
        """

        :param db:
        """
        self.db = db
        self.head_block_header = None
        self.head_state = None

    def _get_head_state(self):
        """Get head state.

        :return:
        """
        if not self.head_state:
            root = self._get_head_block().state_root
            self.head_state = State(self.db, root)
        return self.head_state

    def _get_account(self, address):
        """Get account by address.

        :param address:
        :return:
        """
        state = self._get_head_state()
        account_address = binascii.a2b_hex(utils.remove_0x_head(address))
        return state.get_and_cache_account(account_address)

    def _get_block_hash(self, number):
        """Get block hash by block number.

        :param number:
        :return:
        """
        num = _format_block_number(number)
        hash_key = header_prefix + num + num_suffix
        return self.db.get(hash_key)

    def _get_head_block(self):
        """Get head block header.

        :return:
        """
        if not self.head_block_header:
            block_hash = self.db.get(head_header_key)
            num = self._get_block_number(block_hash)
            self.head_block_header = self._get_block_header(block_hash, num)
            # find header with valid state
            while (
                not self.db.get(self.head_block_header.state_root)
                and self.head_block_header.prevhash is not None
            ):
                block_hash = self.head_block_header.prevhash
                num = self._get_block_number(block_hash)
                self.head_block_header = self._get_block_header(block_hash, num)

        return self.head_block_header

    def _get_block_number(self, block_hash):
        """Get block number by its hash.

        :param block_hash:
        :return:
        """
        number_key = block_hash_prefix + block_hash
        return self.db.get(number_key)

    def _get_block_header(self, block_hash, num):
        """Get block header by block header hash & number.

        :param block_hash:
        :param num:
        :return:
        """
        header_key = header_prefix + num + block_hash

        block_header_data = self.db.get(header_key)
        header = rlp.decode(block_header_data, sedes=BlockHeader)
        return header

    def _get_address_by_hash(self, block_hash):
        """Get mapped address by its hash.

        :param block_hash:
        :return:
        """
        address_key = address_prefix + block_hash
        return self.db.get(address_key)

    def _get_last_indexed_number(self):
        """Get latest indexed block number.

        :return:
        """
        return self.db.get(address_mapping_head_key)

    def _get_block_receipts(self, block_hash, num):
        """Get block transaction receipts by block header hash & number.

        :param block_hash:
        :param num:
        :return:
        """
        number = _format_block_number(num)
        receipts_key = block_receipts_prefix + number + block_hash
        receipts_data = self.db.get(receipts_key)
        receipts = rlp.decode(receipts_data, sedes=CountableList(ReceiptForStorage))
        return receipts


class LevelDBWriter(object):
    """level db writing interface."""

    def __init__(self, db):
        """

        :param db:
        """
        self.db = db
        self.wb = None

    def _set_last_indexed_number(self, number):
        """Set latest indexed block number.

        :param number:
        :return:
        """
        return self.db.put(address_mapping_head_key, _format_block_number(number))

    def _start_writing(self):
        """Start writing a batch."""
        self.wb = self.db.write_batch()

    def _commit_batch(self):
        """Commit a batch."""
        self.wb.write()

    def _store_account_address(self, address):
        """Get block transaction receipts by block header hash & number.

        :param address:
        """
        address_key = address_prefix + utils.sha3(address)
        self.wb.put(address_key, address)


class EthLevelDB(object):
    """Go-Ethereum LevelDB client class."""

    def __init__(self, path):
        """

        :param path:
        """
        self.path = path
        self.db = ETH_DB(path)
        self.reader = LevelDBReader(self.db)
        self.writer = LevelDBWriter(self.db)

    def get_contracts(self):
        """Iterate through all contracts."""
        for account in self.reader._get_head_state().get_all_accounts():
            if account.code is not None:
                code = _encode_hex(account.code)
                contract = EVMContract(code, enable_online_lookup=False)

                yield contract, account.address, account.balance

    def search(self, expression, callback_func):
        """Search through all contract accounts.

        :param expression:
        :param callback_func:
        """
        cnt = 0
        indexer = AccountIndexer(self)

        for contract, address_hash, balance in self.get_contracts():

            if contract.matches_expression(expression):

                try:
                    address = _encode_hex(indexer.get_contract_by_hash(address_hash))
                except AddressNotFoundError:
                    """The hash->address mapping does not exist in our index.

                    If the index is up-to-date, this likely means that
                    the contract was created by an internal transaction.
                    Skip this contract as right now we don't have a good
                    solution for this.
                    """

                    continue

                callback_func(contract, address, balance)

            cnt += 1

            if not cnt % 1000:
                log.info("Searched %d contracts" % cnt)

    def contract_hash_to_address(self, contract_hash):
        """Try to find corresponding account address.

        :param contract_hash:
        :return:
        """

        address_hash = binascii.a2b_hex(utils.remove_0x_head(contract_hash))
        indexer = AccountIndexer(self)

        return _encode_hex(indexer.get_contract_by_hash(address_hash))

    def eth_getBlockHeaderByNumber(self, number):
        """Get block header by block number.

        :param number:
        :return:
        """
        block_hash = self.reader._get_block_hash(number)
        block_number = _format_block_number(number)
        return self.reader._get_block_header(block_hash, block_number)

    def eth_getBlockByNumber(self, number):
        """Get block body by block number.

        :param number:
        :return:
        """
        block_hash = self.reader._get_block_hash(number)
        block_number = _format_block_number(number)
        body_key = body_prefix + block_number + block_hash
        block_data = self.db.get(body_key)
        body = rlp.decode(block_data, sedes=Block)
        return body

    def eth_getCode(self, address):
        """Get account code.

        :param address:
        :return:
        """
        account = self.reader._get_account(address)
        return _encode_hex(account.code)

    def eth_getBalance(self, address):
        """Get account balance.

        :param address:
        :return:
        """
        account = self.reader._get_account(address)
        return account.balance

    def eth_getStorageAt(self, address, position):
        """Get account storage data at position.

        :param address:
        :param position:
        :return:
        """
        account = self.reader._get_account(address)
        return _encode_hex(
            utils.zpad(utils.encode_int(account.get_storage_data(position)), 32)
        )
