import collections.abc
import functools
import sys
from collections import namedtuple
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Type, TypeVar, Union, cast

import torch
from torch import Tensor

from ._core import _unravel_index

__all__ = ["assert_equal", "assert_close"]


# The UsageError should be raised in case the test function is not used correctly. With this the user is able to
# differentiate between a test failure (there is a bug in the tested code) and a test error (there is a bug in the
# test). If pytest is the test runner, we use the built-in UsageError instead our custom one.

try:
    # The module 'pytest' will be imported if the 'pytest' runner is used. This will only give false-positives in case
    # a previously imported module already directly or indirectly imported 'pytest', but the test is run by another
    # runner such as 'unittest'.
    # 'mypy' is not able to handle this within a type annotation
    # (see https://mypy.readthedocs.io/en/latest/common_issues.html#variables-vs-type-aliases for details). In case
    # 'UsageError' is used in an annotation, add a 'type: ignore[valid-type]' comment.
    UsageError: Type[Exception] = sys.modules["pytest"].UsageError  # type: ignore[attr-defined]
except (KeyError, AttributeError):

    class UsageError(Exception):  # type: ignore[no-redef]
        pass


# This is copy-pasted from torch.testing._internal.common_utils.TestCase.dtype_precisions. With this we avoid a
# dependency on torch.testing._internal at import. See
# https://github.com/pytorch/pytorch/pull/54769#issuecomment-813174256 for details.
# {dtype: (rtol, atol)}
_DTYPE_PRECISIONS = {
    torch.float16: (0.001, 1e-5),
    torch.bfloat16: (0.016, 1e-5),
    torch.float32: (1.3e-6, 1e-5),
    torch.float64: (1e-7, 1e-7),
    torch.complex32: (0.001, 1e-5),
    torch.complex64: (1.3e-6, 1e-5),
    torch.complex128: (1e-7, 1e-7),
}


def _get_default_rtol_and_atol(actual: Tensor, expected: Tensor) -> Tuple[float, float]:
    dtype = actual.dtype if actual.dtype == expected.dtype else torch.promote_types(actual.dtype, expected.dtype)
    return _DTYPE_PRECISIONS.get(dtype, (0.0, 0.0))


def _check_are_tensors(actual: Any, expected: Any) -> Optional[AssertionError]:
    """Checks if both inputs are tensors.

    Args:
        actual (Any): Actual input.
        expected (Any): Actual input.

    Returns:
        (Optional[AssertionError]): If check did not pass.
    """
    if not (isinstance(actual, Tensor) and isinstance(expected, Tensor)):
        return AssertionError(f"Both inputs have to be tensors, but got {type(actual)} and {type(expected)} instead.")

    return None


def _check_supported_tensors(
    actual: Tensor,
    expected: Tensor,
) -> Optional[UsageError]:  # type: ignore[valid-type]
    """Checks if the tensors are supported by the current infrastructure.

    All checks are temporary and will be relaxed in the future.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.

    Returns:
        (Optional[UsageError]): If check did not pass.
    """
    if any(t.dtype in (torch.complex32, torch.complex64, torch.complex128) for t in (actual, expected)):
        return UsageError("Comparison for complex tensors is not supported yet.")
    if any(t.is_quantized for t in (actual, expected)):
        return UsageError("Comparison for quantized tensors is not supported yet.")
    if any(t.is_sparse for t in (actual, expected)):
        return UsageError("Comparison for sparse tensors is not supported yet.")

    return None


def _check_attributes_equal(
    actual: Tensor,
    expected: Tensor,
    *,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> Optional[AssertionError]:
    """Checks if the attributes of two tensors match.

    Always checks the :attr:`~torch.Tensor.shape`. Checks for :attr:`~torch.Tensor.device`,
    :attr:`~torch.Tensor.dtype`, and :meth:`~torch.Tensor.stride` are optional and can be disabled.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        check_device (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` are on the
            same :attr:`~torch.Tensor.device` memory.
        check_dtype (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` have the same
            :attr:`~torch.Tensor.dtype`.
        check_stride (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` have the same
            :meth:`~torch.Tensor.stride`.

    Returns:
        (Optional[AssertionError]): If checks did not pass.
    """
    msg_fmtstr = "The values for attribute '{}' do not match: {} != {}."

    if actual.shape != expected.shape:
        return AssertionError(msg_fmtstr.format("shape", actual.shape, expected.shape))

    if check_device and actual.device != expected.device:
        return AssertionError(msg_fmtstr.format("device", actual.device, expected.device))

    if check_dtype and actual.dtype != expected.dtype:
        return AssertionError(msg_fmtstr.format("dtype", actual.dtype, expected.dtype))

    if check_stride and actual.stride() != expected.stride():
        return AssertionError(msg_fmtstr.format("stride()", actual.stride(), expected.stride()))

    return None


def _equalize_attributes(actual: Tensor, expected: Tensor) -> Tuple[Tensor, Tensor]:
    """Equalizes some attributes of two tensors for value comparison.

    If :attr:`actual` and :attr:`expected`
    - are not onn the same memory :attr:`~torch.Tensor.device`, they are moved CPU memory, and
    - do not have the same :attr:`~torch.Tensor.dtype`, they are copied to the :class:`~torch.dtype` returned by
        :func:`torch.promote_types`.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.

    Returns:
        Tuple(Tensor, Tensor): Equalized tensors.
    """
    if actual.device != expected.device:
        actual = actual.cpu()
        expected = expected.cpu()

    if actual.dtype != expected.dtype:
        dtype = torch.promote_types(actual.dtype, expected.dtype)
        actual = actual.to(dtype)
        expected = expected.to(dtype)

    return actual, expected


_Trace = namedtuple(
    "_Trace",
    (
        "total_elements",
        "total_mismatches",
        "mismatch_ratio",
        "max_abs_diff",
        "max_abs_diff_idx",
        "max_rel_diff",
        "max_rel_diff_idx",
    ),
)


def _trace_mismatches(actual: Tensor, expected: Tensor, mismatches: Tensor) -> _Trace:
    """Traces mismatches.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        mismatches (Tensor): Boolean mask of the same shape as :attr:`actual` and :attr:`expected` that indicates
            the location of mismatches.

    Returns:
        (NamedTuple): Mismatch diagnostics with the following fields:

            - total_elements (int): Total number of values.
            - total_mismatches (int): Total number of mismatches.
            - mismatch_ratio (float): Quotient of total mismatches and total elements.
            - max_abs_diff (Union[int, float]): Greatest absolute difference of :attr:`actual` and :attr:`expected`.
            - max_abs_diff_idx (Union[int, Tuple[int, ...]]): Index of greatest absolute difference.
            - max_rel_diff (Union[int, float]): Greatest relative difference of :attr:`actual` and :attr:`expected`.
            - max_rel_diff_idx (Union[int, Tuple[int, ...]]): Index of greatest relative difference.

            The returned type of ``max_abs_diff`` and ``max_rel_diff`` depends on the :attr:`~torch.Tensor.dtype` of
            :attr:`actual` and :attr:`expected`.
    """
    total_elements = mismatches.numel()
    total_mismatches = torch.sum(mismatches).item()
    mismatch_ratio = total_mismatches / total_elements

    dtype = torch.float64 if actual.dtype.is_floating_point else torch.int64
    a_flat = actual.flatten().to(dtype)
    b_flat = expected.flatten().to(dtype)

    abs_diff = torch.abs(a_flat - b_flat)
    max_abs_diff, max_abs_diff_flat_idx = torch.max(abs_diff, 0)

    rel_diff = abs_diff / torch.abs(b_flat)
    max_rel_diff, max_rel_diff_flat_idx = torch.max(rel_diff, 0)

    return _Trace(
        total_elements=total_elements,
        total_mismatches=total_mismatches,
        mismatch_ratio=mismatch_ratio,
        max_abs_diff=max_abs_diff.item(),
        max_abs_diff_idx=_unravel_index(max_abs_diff_flat_idx.item(), mismatches.shape),
        max_rel_diff=max_rel_diff.item(),
        max_rel_diff_idx=_unravel_index(max_rel_diff_flat_idx.item(), mismatches.shape),
    )


def _check_values_equal(actual: Tensor, expected: Tensor) -> Optional[AssertionError]:
    """Checks if the values of two tensors are bitwise equal.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.

    Returns:
        (Optional[AssertionError]): If check did not pass.
    """
    mismatches = torch.ne(actual, expected)
    if not torch.any(mismatches):
        return None

    trace = _trace_mismatches(actual, expected, mismatches)
    return AssertionError(
        f"Tensors are not equal!\n\n"
        f"Mismatched elements: {trace.total_mismatches} / {trace.total_elements} ({trace.mismatch_ratio:.1%})\n"
        f"Greatest absolute difference: {trace.max_abs_diff} at {trace.max_abs_diff_idx}\n"
        f"Greatest relative difference: {trace.max_rel_diff} at {trace.max_rel_diff_idx}"
    )


def _check_values_close(
    actual: Tensor,
    expected: Tensor,
    *,
    rtol,
    atol,
) -> Optional[AssertionError]:
    """Checks if the values of two tensors are close up to a desired tolerance.

    Args:
        actual (Tensor): Actual tensor.
        expected (Tensor): Expected tensor.
        rtol (float): Relative tolerance.
        atol (float): Absolute tolerance.

    Returns:
        (Optional[AssertionError]): If check did not pass.
    """
    mismatches = ~torch.isclose(actual, expected, rtol=rtol, atol=atol)
    if not torch.any(mismatches):
        return None

    trace = _trace_mismatches(actual, expected, mismatches)
    return AssertionError(
        f"Tensors are not close!\n\n"
        f"Mismatched elements: {trace.total_mismatches} / {trace.total_elements} ({trace.mismatch_ratio:.1%})\n"
        f"Greatest absolute difference: {trace.max_abs_diff} at {trace.max_abs_diff_idx} (up to {atol} allowed)\n"
        f"Greatest relative difference: {trace.max_rel_diff} at {trace.max_rel_diff_idx} (up to {rtol} allowed)"
    )


def _check_tensors_equal(
    actual: Tensor,
    expected: Tensor,
    *,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> Optional[Exception]:
    """Checks that the values of two tensors are bitwise equal.

    Optionally, checks that some attributes of both tensors are equal.

    For a description of the parameters see :func:`assert_equal`.

    Returns:
        Optional[Exception]: If checks did not pass.
    """
    exc: Optional[Exception] = _check_are_tensors(actual, expected)
    if exc:
        return exc

    exc = _check_supported_tensors(actual, expected)
    if exc:
        return exc

    exc = _check_attributes_equal(
        actual, expected, check_device=check_device, check_dtype=check_dtype, check_stride=check_stride
    )
    if exc:
        return exc
    actual, expected = _equalize_attributes(actual, expected)

    exc = _check_values_equal(actual, expected)
    if exc:
        return exc

    return None


def _check_tensors_close(
    actual: Tensor,
    expected: Tensor,
    *,
    rtol: Optional[float] = None,
    atol: Optional[float] = None,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> Optional[Exception]:
    r"""Checks that the values of two tensors are close.

    Closeness is defined by

    .. math::

        \lvert a - b \rvert \le \texttt{atol} + \texttt{rtol} \cdot \lvert b \rvert

    If both tolerances, :attr:`rtol` and :attr:`rtol`, are ``0``, asserts that :attr:`actual` and :attr:`expected` are
    bitwise equal.

    Optionally, checks that some attributes of both tensors are equal.

    For a description of the parameters see :func:`assert_equal`.

    Returns:
        Optional[Exception]: If checks did not pass.
    """
    exc: Optional[Exception] = _check_are_tensors(actual, expected)
    if exc:
        return exc

    exc = _check_supported_tensors(actual, expected)
    if exc:
        return exc

    if (rtol is None) ^ (atol is None):
        # We require both tolerance to be omitted or specified, because specifying only one might lead to surprising
        # results. Imagine setting atol=0.0 and the tensors still match because rtol>0.0.
        return UsageError(
            f"Both 'rtol' and 'atol' must be omitted or specified, " f"but got rtol={rtol} and atol={atol} instead."
        )
    elif rtol is None:
        rtol, atol = _get_default_rtol_and_atol(actual, expected)

    exc = _check_attributes_equal(
        actual, expected, check_device=check_device, check_dtype=check_dtype, check_stride=check_stride
    )
    if exc:
        raise exc
    actual, expected = _equalize_attributes(actual, expected)

    if (rtol == 0.0) and (atol == 0.0):
        exc = _check_values_equal(actual, expected)
    else:
        exc = _check_values_close(actual, expected, rtol=rtol, atol=atol)
    if exc:
        return exc

    return None


def _check_by_type(
    actual: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    expected: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    check_tensors: Callable[[Tensor, Tensor], Optional[Exception]],
) -> Optional[Exception]:
    """Delegates tensor checking based on the inputs types.

    Currently supports pairs of

    - :class:`Tensor`'s,
    - :class:`~collections.abc.Sequence`'s of :class:`Tensor`'s, and
    - :class:`~collections.abc.Mapping`'s of :class:`Tensor`'s.

    Args:
        actual (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Actual input.
        expected (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Expected input.
        check_tensors (Callable[[Tensor, Tensor], Optional[Exception]]): Callable used to check if two tensors match.
            In case they mismatch should return an :class:`Exception` with an expressive error message.

    Returns:
        (Optional[Exception]): :class:`UsageError` if the inputs types are unsupported. Additionally, any exception
        returned by :attr:`check_tensors`.
    """
    # _check_are_tensors() returns nothing in case both inputs are tensors and an exception otherwise. Thus, the logic
    # is inverted here.
    are_tensors = not _check_are_tensors(actual, expected)
    if are_tensors:
        return check_tensors(cast(Tensor, actual), cast(Tensor, expected))

    if isinstance(actual, collections.abc.Sequence) and isinstance(expected, collections.abc.Sequence):
        return _check_sequence(actual, expected, check_tensors)
    elif isinstance(actual, collections.abc.Mapping) and isinstance(expected, collections.abc.Mapping):
        return _check_mapping(actual, expected, check_tensors)

    return UsageError(
        f"Both inputs have to be tensors, or sequences or mappings of tensors, "
        f"but got {type(actual)} and {type(expected)} instead."
    )


E = TypeVar("E", bound=Exception)


def _amend_error_message(exc: E, msg_fmtstr: str) -> E:
    """Amends an exception message.

    Args:
        exc (E): Exception.
        msg_fmtstr: Format string for the amended message.

    Returns:
        (E): New exception with amended error message.
    """
    return type(exc)(msg_fmtstr.format(str(exc)))


_SEQUENCE_MSG_FMTSTR = "The failure occurred at index {} of the sequences."


def _check_sequence(
    actual: Sequence[Tensor], expected: Sequence[Tensor], check_tensors: Callable[[Tensor, Tensor], Optional[Exception]]
) -> Optional[Exception]:
    """Checks if the values of two sequences of tensors match.

    Args:
        actual (Sequence[Tensor]): Actual sequence of tensors.
        expected (Sequence[Tensor]): Expected sequence of tensors.
        check_tensors (Callable[[Tensor, Tensor], Optional[Exception]]): Callable used to check if the items of
            :attr:`actual` and :attr:`expected` match. In case they mismatch should return an :class:`Exception` with
            an expressive error message.

    Returns:
        Optional[Exception]: :class:`AssertionError` if the sequences do not have the same length. Additionally, any
            exception returned by :attr:`check_tensors`. In this case, the error message is amended to include the
            first offending index.
    """
    actual_len = len(actual)
    expected_len = len(expected)
    if actual_len != expected_len:
        return AssertionError(f"The length of the sequences mismatch: {actual_len} != {expected_len}")
    for idx, (actual_t, expected_t) in enumerate(zip(actual, expected)):
        exc = check_tensors(actual_t, expected_t)
        if exc:
            return _amend_error_message(exc, f"{{}}\n\n{_SEQUENCE_MSG_FMTSTR.format(idx)}")

    return None


_MAPPING_MSG_FMTSTR = "The failure occurred for key '{}' of the mappings."


def _check_mapping(
    actual: Mapping[Any, Tensor],
    expected: Mapping[Any, Tensor],
    check_tensors: Callable[[Tensor, Tensor], Optional[Exception]],
) -> Optional[Exception]:
    """Checks if the values of two mappings of tensors match.

    Args:
        actual (Mapping[Any, Tensor]): First mapping of tensors.
        expected (Mapping[Any, Tensor]): Second mapping of tensors.
        check_tensors (Callable[[Tensor, Tensor], Optional[Exception]]): Callable used to check if the values of
            :attr:`actual` and :attr:`expected` match. In case they mismatch should return an :class:`Exception` with
            an expressive error message.

    Returns:
        Optional[Exception]: :class:`AssertionError` if the sequences do not have the same set of keys. Additionally,
            any exception returned by :attr:`check_tensors`. In this case, the error message is amended to include the
            first offending key.
    """
    actual_keys = set(actual.keys())
    expected_keys = set(expected.keys())
    if actual_keys != expected_keys:
        missing_keys = expected_keys - actual_keys
        additional_keys = actual_keys - expected_keys
        return AssertionError(
            f"The keys of the mappings do not match:\n\n"
            f"Missing keys in the actual mapping: {sorted(missing_keys)}\n"
            f"Additional keys in the actual mapping: {sorted(additional_keys)}\n"
        )
    for key in sorted(actual_keys):
        actual_t = actual[key]
        expected_t = expected[key]

        exc = check_tensors(actual_t, expected_t)
        if exc:
            return _amend_error_message(exc, f"{{}}\n\n{_MAPPING_MSG_FMTSTR.format(key)}")

    return None


def assert_equal(
    actual: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    expected: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    *,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> None:
    """Asserts that the values of tensors are bitwise equal.

    Optionally, checks that some attributes of tensors are equal.

    Also supports :class:`~collections.abc.Sequence`'s and :class:`~collections.abc.Mapping`'s of :class:`Tensor`'s.

    Args:
        actual (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Actual input.
        expected (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Expected input.
        check_device (bool): If ``True`` (default), asserts that tensors live in the same :attr:`~torch.Tensor.device`
            memory. If this check is disabled **and** they do not live in the same memory :attr:`~torch.Tensor.device`,
            they are moved CPU memory before their values are compared.
        check_dtype (bool): If ``True`` (default), asserts that tensors have the same :attr:`~torch.Tensor.dtype`. If
            this check is disabled they do not have the same :attr:`~torch.Tensor.dtype`, they are copied to the
            :class:`~torch.dtype` returned by :func:`torch.promote_types` before their values are compared.
        check_stride (bool): If ``True`` (default), asserts that the tensors have the same stride.

    Raises:
        UsageError: If the input pair has an unsupported type.
        UsageError: If any tensor is complex, quantized, or sparse. This is a temporary restriction and
            will be relaxed in the future.
        AssertionError: If any corresponding tensors do not have the same :attr:`~torch.Tensor.shape`.
        AssertionError: If :attr:`check_device`, but any corresponding tensors do not live in the same
            :attr:`~torch.Tensor.device` memory.
        AssertionError: If :attr:`check_dtype`, but any corresponding tensors do not have the same
            :attr:`~torch.Tensor.dtype`.
        AssertionError: If :attr:`check_stride`, but any corresponding tensors do not have the same stride.
        AssertionError: If the values of any corresponding tensors are not bitwise equal.
        AssertionError: If the inputs are :class:`~collections.abc.Sequence`'s, but their length does not match.
        AssertionError: If the inputs are :class:`~collections.abc.Mapping`'s, but their set of keys mismatch.

    .. seealso::

        To assert that the values in tensors are close but are not required to be bitwise equal, use
        :func:`assert_close` instead.
    """
    check_tensors = functools.partial(
        _check_tensors_equal,
        check_device=check_device,
        check_dtype=check_dtype,
        check_stride=check_stride,
    )
    exc = _check_by_type(actual, expected, check_tensors)
    if exc:
        raise exc


def assert_close(
    actual: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    expected: Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]],
    *,
    rtol: Optional[float] = None,
    atol: Optional[float] = None,
    check_device: bool = True,
    check_dtype: bool = True,
    check_stride: bool = True,
) -> None:
    r"""Asserts that the values of tensors are close.

    Closeness is defined by

    .. math::

        \lvert a - b \rvert \le \texttt{atol} + \texttt{rtol} \cdot \lvert b \rvert

    If both tolerances, :attr:`rtol` and :attr:`rtol`, are ``0``, asserts that :attr:`actual` and :attr:`expected` are
    bitwise equal.

    Optionally, checks that some attributes of tensors are equal.

    Also supports :class:`~collections.abc.Sequence`'s and :class:`~collections.abc.Mapping`'s of :class:`Tensor`'s.

    Args:
        actual (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Actual input.
        expected (Union[Tensor, Sequence[Tensor], Mapping[Any, Tensor]]): Expected input.
        rtol (Optional[float]): Relative tolerance. If specified :attr:`atol` must also be specified. If omitted,
            default values based on the :attr:`~torch.Tensor.dtype` are selected with the below table.
        atol (Optional[float]): Absolute tolerance. If specified :attr:`rtol` must also be specified. If omitted,
            default values based on the :attr:`~torch.Tensor.dtype` are selected with the below table.
        check_device (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` are on the
            same :attr:`~torch.Tensor.device` memory. If this check is disabled **and** :attr:`actual` and
            :attr:`expected` are not on the same memory :attr:`~torch.Tensor.device`, they are moved CPU memory before
            their values are compared.
        check_dtype (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` have the same
            :attr:`~torch.Tensor.dtype`. If this check is disabled **and** :attr:`actual` and :attr:`expected` do not
            have the same :attr:`~torch.Tensor.dtype`, they are copied to the :class:`~torch.dtype` returned by
            :func:`torch.promote_types` before their values are compared.
        check_stride (bool): If ``True`` (default), asserts that both :attr:`actual` and :attr:`expected` have the same
            stride.

    Raises:
        UsageError: If the input pair has an unsupported type.
        UsageError: If :attr:`actual` or :attr:`expected` is complex, quantized, or sparse. This is a temporary
            restriction and will be relaxed in the future.
        AssertionError: If :attr:`actual` and :attr:`expected` do not have the same :attr:`~torch.Tensor.shape`.
        AssertionError: If :attr:`check_device`, but :attr:`actual` and :attr:`expected` are not on the same
            :attr:`~torch.Tensor.device` memory.
        AssertionError: If :attr:`check_dtype`, but :attr:`actual` and :attr:`expected` do not have the same
            :attr:`~torch.Tensor.dtype`.
        AssertionError: If :attr:`check_stride`, but :attr:`actual` and :attr:`expected` do not have the same stride.
        AssertionError: If the values of :attr:`actual` and :attr:`expected` are close up to a desired tolerance.
        AssertionError: If the inputs are :class:`~collections.abc.Sequence`'s, but their length does not match.
        AssertionError: If the inputs are :class:`~collections.abc.Mapping`'s, but their set of keys mismatch.

    The following table displays the default ``rtol``'s and ``atol``'s. Note that the :class:`~torch.dtype` refers to
    the promoted type in case :attr:`actual` and :attr:`expected` do not have the same :attr:`~torch.Tensor.dtype`.

    +===========================+============+==========+
    | :class:`~torch.dtype`     | ``rtol``   | ``atol`` |
    +===========================+============+==========+
    | :attr:`~torch.float16`    | ``1e-3``   | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.bfloat16`   | ``1.6e-2`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.float32`    | ``1.3e-6`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.float64`    | ``1e-7``   | ``1e-7`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex32`  | ``1e-3``   | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex64`  | ``1.3e-6`` | ``1e-5`` |
    +---------------------------+------------+----------+
    | :attr:`~torch.complex128` | ``1e-7``   | ``1e-7`` |
    +---------------------------+------------+----------+
    | other                     | ``0.0``    | ``0.0``  |
    +---------------------------+------------+----------+

    .. seealso::

        To assert that the values in tensors are bitwise equal, use :func:`assert_equal` instead.
    """
    check_tensors = functools.partial(
        _check_tensors_close,
        rtol=rtol,
        atol=atol,
        check_device=check_device,
        check_dtype=check_dtype,
        check_stride=check_stride,
    )
    exc = _check_by_type(actual, expected, check_tensors)
    if exc:
        raise exc
