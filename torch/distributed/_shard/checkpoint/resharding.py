import hashlib
import io
from typing import List, Tuple, Dict

import torch
from torch import Tensor

from torch.distributed._shard.sharded_tensor import (
    ShardedTensor,
)
from torch.distributed._shard.sharding_spec import (
    ShardMetadata,
)
from torch.distributed._shard.sharding_spec._internals import (
    _check_shard_metadata_pair_overlap,
)
from torch.distributed._shard.sharded_tensor.shard import Shard
from torch.distributed._shard.sharded_tensor.metadata import TensorProperties


from .metadata import (
    BytesWriteRequest,
    TensorReadRequest,
    TensorWriteRequest,
    ChunkStorageMetadata,
    TensorStorageMetadata,
    BytesStorageMetadata,
    MetadataIndex,
)

def _trim(tensor: torch.Tensor) -> torch.Tensor:
    tensor = tensor.detach()
    if tensor.storage().size() != tensor.numel():
        return tensor.clone()
    return tensor

def _create_storage_key(
    storage_key_to_fqn: Dict[str, str],
    fqn: str
) -> str:
    """
    Compute the storage key from the Fully Qualified Name
    Storage keys must respect the following properties:
    1) Globally unique name across all objects and ranks.
    2) Suitable for usage with common storage systems (IE, alphanumeric only)
    """

    storage_key = hashlib.sha256(bytes(fqn, "utf-8")).hexdigest()
    counter = 0
    while storage_key in storage_key_to_fqn:
        storage_key = hashlib.sha256(bytes(f"{fqn}{counter}", "utf-8")).hexdigest()
        counter += 1

    storage_key_to_fqn[storage_key] = fqn
    return storage_key

# This constant is used as the separator character between tensor name and shard name
STORAGE_KEY_SEPARATOR = "$"

def _shards_get_overlap_region_wrt_saved_tensor(
    saved_shard: ShardMetadata, current_shard: ShardMetadata
) -> List[Tuple[int, int, int, int]]:
    """
    Return the overlapping region between saved_shard and current_shard.
    There returned list has the same number of elements as the tensor's dimension.
    For each element, we produce a tuple with the following contents:
        (dimension, `saved_shard` offset, `current_shard` offset, length)

    Offsets are relative to each shard.
    """
    narrows = []
    for dim, (
        saved_shard_offset,
        current_shard_offset,
        saved_shard_size,
        current_shard_size,
    ) in enumerate(
        zip(
            saved_shard.shard_offsets,
            current_shard.shard_offsets,
            saved_shard.shard_sizes,
            current_shard.shard_sizes,
        )
    ):
        min_range_end = min(
            saved_shard_offset + saved_shard_size,
            current_shard_offset + current_shard_size,
        )

        length = min_range_end - max(current_shard_offset, saved_shard_offset)

        if saved_shard_offset > current_shard_offset:
            offset_for_saved_tensor = 0
            offset_for_current_tensor = saved_shard_offset - current_shard_offset
        else:
            offset_for_saved_tensor = current_shard_offset - saved_shard_offset
            offset_for_current_tensor = 0

        narrows.append(
            (dim, offset_for_saved_tensor, offset_for_current_tensor, length)
        )

    return narrows

def _chunk_to_shard_md(chunk_md: ChunkStorageMetadata) -> ShardMetadata:
    return ShardMetadata(
        shard_offsets=list(chunk_md.offsets),
        shard_sizes=list(chunk_md.sizes)
    )

def _shard_md_to_chunk(chunk_md: ShardMetadata) -> ChunkStorageMetadata:
    return ChunkStorageMetadata(
        offsets=torch.Size(chunk_md.shard_offsets),
        sizes=torch.Size(chunk_md.shard_sizes),
    )

def _compute_sharded_tensor_md(
    tensor: ShardedTensor,
) -> TensorStorageMetadata:
    smd = [_shard_md_to_chunk(sm) for sm in tensor.metadata().shards_metadata]

    return TensorStorageMetadata(
        properties=tensor.metadata().tensor_properties,
        size=torch.Size(tensor.metadata().size),
        chunks=smd,
    )


def _get_shard_key(shard: ShardMetadata) -> str:
    """
    Compute an unique key for a shard.

    This key is unique vis-a-vis other shard of the owning ShardedTensor
    """
    return "_".join(str(i) for i in shard.shard_offsets)

def _get_shard_storage_key(
    tensor_storage_key: str,
    shard: ShardMetadata,
    storage_key_to_fqn: Dict[str, str]
) -> str:
    shard_key = f"{tensor_storage_key}{STORAGE_KEY_SEPARATOR}{_get_shard_key(shard)}"

    return _create_storage_key(storage_key_to_fqn, shard_key)


def _prepare_sharded_tensor_write(
    fqn: str,
    sharded_tensor: ShardedTensor,
    storage_key: str,
    storage_key_to_fqn: Dict[str, str]
) -> Tuple[List[TensorWriteRequest], TensorStorageMetadata, Dict[MetadataIndex, str]]:
    """
    Prepare sharded tensor write.

    Args:
        fqn: The FQN of ``sharded_tensor`` in the state_dict.
        sharded_tensor: The sharded tensor to persist.
        storage_key: The identifier for `sharded_tensor`.
        storage_key_to_fqn: dict used to produce storage keys
    Returns:
        A 3-element tuple with the following values:
            List of ``TensorWriteRequest`` for the tensor
            Metadada describing the tensor.
            Dictionary describing storage information for this tensor

    NB `storage_key` is used to compose the key names of the local shards.
    """
    write_requests = []
    shard_to_storage_key: Dict[str, str] = {}
    storage_md = {}

    for shard_md in sharded_tensor.metadata().shards_metadata:
        shard_storage_key = _get_shard_storage_key(storage_key, shard_md, storage_key_to_fqn)
        shard_to_storage_key[_get_shard_key(shard_md)] = shard_storage_key
        storage_md[MetadataIndex(fqn, shard_md.shard_offsets)] = shard_storage_key

    for shard in sharded_tensor.local_shards():
        tensor = shard.tensor.detach()
        shard_storage_key = shard_to_storage_key[_get_shard_key(shard.metadata)]

        wr = TensorWriteRequest(
            tensor=_trim(tensor),
            storage_key=shard_storage_key,
        )
        write_requests.append(wr)
    return write_requests, _compute_sharded_tensor_md(sharded_tensor), storage_md


def _prepare_sharded_tensor_read(
    fqn: str,
    storage_metadata: Dict[MetadataIndex, str],
    metadata: TensorStorageMetadata,
    sharded_tensor_out: ShardedTensor
) -> List[TensorReadRequest]:
    """
    Prepare sharded tensor read.

    Args:
        fqn: The FQN of ``sharded_tensor`` in the state_dict.
        storage_metadata: Dictionary describing checkpoint storage.
        metadata: Metadata describing the persisted sharded tensor. Normally,
                  this is generated by func::`_prepare_sharded_tensor_write`.
        sharded_tensor_out: The ShardedTensor being read.

    Returns:
        A list of class::`TensorReadRequest`. When fullfilled,
        `sharded_tensor_out`'s local shards load from the persisted sharded
        tensor.
    """
    return _prepare_generic_tensor_read(
        fqn,
        metadata.chunks,
        sharded_tensor_out.local_shards(),
        storage_metadata)

def _prepare_generic_tensor_read(
    fqn: str,
    checkpoint_shards: List[ChunkStorageMetadata],
    local_shards: List[Shard],
    storage_metadata: Dict[MetadataIndex, str]
) -> List[TensorReadRequest]:
    read_reqs = []
    # this is a naive quadratic algo that can be optimized later
    for shard in local_shards:
        # scan all mds looking for chunks
        for storage_md in checkpoint_shards:
            shard_md_from_storage = _chunk_to_shard_md(storage_md)

            # do they overlap?
            if not _check_shard_metadata_pair_overlap(
                shard.metadata, shard_md_from_storage
            ):
                continue

            storage_key = storage_metadata[MetadataIndex(fqn, storage_md.offsets)]
            target_tensor = shard.tensor.detach()
            offsets = []
            lengths = []
            for (
                dim,
                offset_for_saved_tensor,
                offset_for_current_tensor,
                length,
            ) in _shards_get_overlap_region_wrt_saved_tensor(
                saved_shard=shard_md_from_storage, current_shard=shard.metadata
            ):
                # Note that we do NOT want to make any tensor copy.
                # all operation must be view only
                target_tensor = torch.narrow(
                    target_tensor, dim, offset_for_current_tensor, length
                )
                offsets.append(offset_for_saved_tensor)
                lengths.append(length)

            read_reqs.append(
                TensorReadRequest(
                    tensor=target_tensor,
                    storage_key=storage_key,
                    offsets=tuple(offsets),
                    lengths=tuple(lengths),
                )
            )
    return read_reqs

def _compute_tensor_md(tensor: Tensor) -> TensorStorageMetadata:
    return TensorStorageMetadata(
        properties=TensorProperties.create_from_tensor(tensor),
        size=tensor.size(),
        chunks=[ChunkStorageMetadata(
            offsets=torch.Size([0] * len(tensor.shape)),
            sizes=tensor.size(),
        )]
    )

def _prepare_tensor_write(
    tensor: Tensor, fqn: str, storage_key_to_fqn: Dict[str, str]
) -> Tuple[List[TensorWriteRequest], TensorStorageMetadata, Dict[MetadataIndex, str]]:
    storage_key = _create_storage_key(storage_key_to_fqn, fqn)
    storage_md = {MetadataIndex(fqn, [0] * len(tensor.shape)): storage_key}
    write_reqs = [
        TensorWriteRequest(
            tensor=_trim(tensor),
            storage_key=storage_key,
        )
    ]
    return (write_reqs, _compute_tensor_md(tensor), storage_md)


def _compute_bytes_md(bytes: io.BytesIO) -> BytesStorageMetadata:
    return BytesStorageMetadata(
    )

def _prepare_bytes_write(
    bytes: io.BytesIO, fqn: str, storage_key_to_fqn: Dict[str, str]
) -> Tuple[List[BytesWriteRequest], BytesStorageMetadata, Dict[MetadataIndex, str]]:
    storage_key = _create_storage_key(storage_key_to_fqn, fqn)
    storage_md = {MetadataIndex(fqn): storage_key}

    write_reqs = [
        BytesWriteRequest(
            bytes=bytes,
            storage_key=storage_key,
        )
    ]
    return (write_reqs, _compute_bytes_md(bytes), storage_md)
