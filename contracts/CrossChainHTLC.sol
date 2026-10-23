// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.30;

import { Initializable } from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import { UUPSUpgradeable } from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import { PausableUpgradeable } from "@openzeppelin/contracts-upgradeable/utils/PausableUpgradeable.sol";
import { ReentrancyGuardUpgradeable } from "@openzeppelin/contracts-upgradeable/utils/ReentrancyGuardUpgradeable.sol";
import { AccessControlEnumerableUpgradeable } from "@openzeppelin/contracts-upgradeable/access/extensions/AccessControlEnumerableUpgradeable.sol";
import { EIP712Upgradeable } from "@openzeppelin/contracts-upgradeable/utils/cryptography/EIP712Upgradeable.sol";
import { ECDSA } from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import { Address } from "@openzeppelin/contracts/utils/Address.sol";
import { ICrossChainHTLC } from "./interfaces/ICrossChainHTLC.sol";
import { ILiquidityPool } from "./interfaces/ILiquidityPool.sol";

contract CrossChainHTLC is
    Initializable,
    UUPSUpgradeable,
    PausableUpgradeable,
    ReentrancyGuardUpgradeable,
    AccessControlEnumerableUpgradeable,
    EIP712Upgradeable,
    ICrossChainHTLC
{
    using Address for address payable;

    // ========= Roles =========
    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant HTLC_MANAGER_ROLE = keccak256("HTLC_MANAGER_ROLE");
    bytes32 public constant CROSS_CHAIN_MANAGER_ROLE = keccak256("CROSS_CHAIN_MANAGER_ROLE");

    // ========= EIP-712 =========
    bytes32 private constant HTLC_LOCK_TYPEHASH =
        keccak256(
            "HtlcLock(bytes32 secretHash,uint256 amount,address token,address user,address sessionAddress,uint64 maintainerDeadline,uint256 nonce)"
        );

    bytes32 private constant HTLC_LOCK_WITH_FEE_TYPEHASH =
        keccak256(
            "HtlcLockWithFee(bytes32 secretHash,uint256 amount,address token,address user,address sessionAddress,uint64 maintainerDeadline,uint256 nonce,uint256 executionFeeNative)"
        );

    bytes32 private constant CLAIM_TYPEHASH = keccak256("Claim(bytes32 secretHash)");

    struct Swap {
        uint32 lockUntil;
        uint8 status;
        bytes27 paramsHash;
    }

    enum Status {
        None,
        SourceLocked,
        DestinationLocked,
        Claimed,
        Refunded
    }

    // ========= Storage =========
    mapping(bytes32 => Swap) public swaps;
    address public pool;
    address public maintainer;
    uint32 public defaultSourceLockSecs;
    uint32 public defaultDestinationLockSecs;

    // ========= Events =========
    event DestinationLocked(bytes32 secretHash, address user, address tokenOut, uint256 amount, uint64 untilTs);
    event Claimed(bytes32 secretHash, address claimer, address token, uint256 amount);
    event Refunded(bytes32 secretHash);
    event MaintainerSet(address maintainer);
    event PoolSet(address pool);
    event DefaultLocksUpdated(uint64 sourceLockSecs, uint64 destinationLockSecs);
    event NativeWithdrawn(address to, uint256 amount, address sender);
    event FeePaid(bytes32 secretHash, uint256 amount);

    // ========= Errors =========
    error InvalidSignature();
    error InvalidStatus();
    error DeadlineExpired();
    error OnlyUser();
    error SecretMismatch();
    error ParamsMismatch();
    error SwapIdAlreadyUsed();
    error LockNotExpired();
    error WrongStatus();
    error FeeMismatch(uint256 expected, uint256 actual);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(
        address admin,
        address crossChainManager,
        address htlcManager,
        address maintainerAddress,
        address poolAddress,
        uint32 sourceDefault,
        uint32 destinationDefault
    ) external initializer {
        __UUPSUpgradeable_init();
        __Pausable_init();
        __ReentrancyGuard_init();
        __AccessControlEnumerable_init();
        __EIP712_init("CrossChainHTLC", "1");

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(CROSS_CHAIN_MANAGER_ROLE, crossChainManager);
        _grantRole(HTLC_MANAGER_ROLE, htlcManager);
        maintainer = maintainerAddress;
        pool = poolAddress;
        defaultSourceLockSecs = sourceDefault;
        defaultDestinationLockSecs = destinationDefault;
    }

    // ========= Admin =========
    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    function setMaintainer(address newMaintainer) external onlyRole(DEFAULT_ADMIN_ROLE) {
        maintainer = newMaintainer;
        emit MaintainerSet(newMaintainer);
    }

    function setPool(address poolAddress) external onlyRole(HTLC_MANAGER_ROLE) {
        pool = poolAddress;
        emit PoolSet(poolAddress);
    }

    function setDefaultLocks(
        uint32 sourceSecs,
        uint32 destinationSecs
    ) external onlyRole(HTLC_MANAGER_ROLE) {
        defaultSourceLockSecs = sourceSecs;
        defaultDestinationLockSecs = destinationSecs;
        emit DefaultLocksUpdated(sourceSecs, destinationSecs);
    }

    function withdrawNative(
        address payable to,
        uint256 amount
    ) external onlyRole(CROSS_CHAIN_MANAGER_ROLE) nonReentrant {
        require(to != address(0), "Invalid recipient");
        require(amount > 0, "Amount must be greater than 0");
        require(amount <= address(this).balance, "Insufficient balance");

        to.sendValue(amount);

        emit NativeWithdrawn(to, amount, msg.sender);
    }

    // ========= Lock =========
    function lockSource(HtlcLockSource calldata params, uint256 executionFeeNative) external payable nonReentrant whenNotPaused {
        _lockSource(params, executionFeeNative);
    }

    function lockSourceWithPermit(
        HtlcLockSource calldata params,
        ILiquidityPool.PermitData calldata permitData
    ) external onlyRole(CROSS_CHAIN_MANAGER_ROLE) nonReentrant whenNotPaused {
        _lockSourceWithPermit(params, permitData);
    }

    function lockSourceWithPermit2(
        HtlcLockSource calldata params,
        ILiquidityPool.Permit2Data calldata permit2Data
    ) external onlyRole(CROSS_CHAIN_MANAGER_ROLE) nonReentrant whenNotPaused {
        _lockSourceWithPermit2(params, permit2Data);
    }

    function lockSourceWithSignature(
        HtlcLockSource calldata params,
        ILiquidityPool.SignatureData calldata signatureData
    ) external onlyRole(CROSS_CHAIN_MANAGER_ROLE) nonReentrant whenNotPaused {
        _lockSourceWithSignature(params, signatureData);
    }

    function lockDestination(
        Base calldata params
    ) external onlyRole(CROSS_CHAIN_MANAGER_ROLE) nonReentrant whenNotPaused {
        _lockDestination(params);
    }

    // ========= Claim =========
    function claimSource(ClaimSource calldata params) external nonReentrant whenNotPaused {
        _claimSource(params);
    }

    function claimDestination(
        ClaimDestination calldata params
    ) external nonReentrant whenNotPaused {
        _claimDestination(params);
    }

    // ========= Refund =========
    function refundSource(RefundSource calldata params) external nonReentrant whenNotPaused {
        _refundSource(params);
    }

    function refundDestination(Base calldata params) external nonReentrant whenNotPaused {
        _refundDestination(params);
    }

    // ========= Internal ========
    function _lockSource(HtlcLockSource calldata htlc, uint256 executionFeeNative) internal {
        if (msg.value != executionFeeNative) revert FeeMismatch(executionFeeNative, msg.value);
        if (msg.sender != htlc.base.user) revert OnlyUser();
        _validateHtlcLockSourceWithFee(
            htlc.base,
            htlc.sessionAddress,
            htlc.nonce,
            htlc.maintainerDeadline,
            executionFeeNative,
            htlc.maintainerSig
        );
        uint32 untilTs = _createSourceSwap(htlc.base, htlc.sessionAddress);
        ILiquidityPool(pool).lockSource(htlc.base, htlc.nonce, untilTs);

        if (msg.value > 0) {
            emit FeePaid(htlc.base.secretHash, msg.value);
        }
    }

    function _lockSourceWithPermit(
        HtlcLockSource calldata htlc,
        ILiquidityPool.PermitData calldata permitData
    ) internal {
        _validateHtlcLockSource(
            htlc.base,
            htlc.sessionAddress,
            htlc.nonce,
            htlc.maintainerDeadline,
            htlc.maintainerSig
        );

        uint32 untilTs = _createSourceSwap(htlc.base, htlc.sessionAddress);
        ILiquidityPool(pool).lockSourceWithPermit(htlc.base, htlc.nonce, untilTs, permitData, htlc.sessionAddress);
    }

    function _lockSourceWithPermit2(
        HtlcLockSource calldata htlc,
        ILiquidityPool.Permit2Data calldata permit2Data
    ) internal {
        _validateHtlcLockSource(
            htlc.base,
            htlc.sessionAddress,
            htlc.nonce,
            htlc.maintainerDeadline,
            htlc.maintainerSig
        );

        uint32 untilTs = _createSourceSwap(htlc.base, htlc.sessionAddress);
        ILiquidityPool(pool).lockSourceWithPermit2(htlc.base, htlc.nonce, untilTs, permit2Data, htlc.sessionAddress);
    }

    function _lockSourceWithSignature(
        HtlcLockSource calldata htlc,
        ILiquidityPool.SignatureData calldata signatureData
    ) internal {
        _validateHtlcLockSource(
            htlc.base,
            htlc.sessionAddress,
            htlc.nonce,
            htlc.maintainerDeadline,
            htlc.maintainerSig
        );

        uint32 untilTs = _createSourceSwap(htlc.base, htlc.sessionAddress);
        ILiquidityPool(pool).lockSourceWithSignature(htlc.base, htlc.nonce, untilTs, signatureData, htlc.sessionAddress);
    }

    function _createSourceSwap(
        Base calldata base,
        address sessionAddress
    ) internal returns (uint32) {
        if (swaps[base.secretHash].status != uint8(Status.None)) revert SwapIdAlreadyUsed();

        uint32 untilTs = uint32(block.timestamp) + defaultSourceLockSecs;

        Swap storage s = swaps[base.secretHash];
        s.lockUntil = untilTs;
        s.status = uint8(Status.SourceLocked);
        s.paramsHash = _hashParamsSource(
            base.secretHash,
            base.amount,
            base.token,
            base.user,
            base.chainId,
            sessionAddress
        );
        return untilTs;
    }

    function _lockDestination(Base calldata params) internal {
        if (block.chainid != params.chainId) revert InvalidStatus();

        uint64 lockSecs = defaultDestinationLockSecs;
        uint64 untilTs = uint64(block.timestamp) + lockSecs;

        Swap storage s = swaps[params.secretHash];
        if (s.status != uint8(Status.None)) revert SwapIdAlreadyUsed();

        s.lockUntil = uint32(untilTs);
        s.status = uint8(Status.DestinationLocked);
        s.paramsHash = _hashParamsDestination(
            params.secretHash,
            params.amount,
            params.token,
            params.user,
            params.chainId
        );

        ILiquidityPool(pool).lockDestination(
            params.secretHash,
            params.token,
            params.amount,
            untilTs
        );
        emit DestinationLocked(params.secretHash, params.user, params.token, params.amount, untilTs);
    }

    function _claimSource(ClaimSource calldata params) internal {
        Swap storage s = swaps[params.base.secretHash];

        _validateClaimSource(params, s);

        bytes32 structHash = keccak256(abi.encode(CLAIM_TYPEHASH, params.base.secretHash));
        address recoveredSessionAddress = ECDSA.recover(
            _hashTypedDataV4(structHash),
            params.secretHashSignature
        );
        if (recoveredSessionAddress != params.sessionAddress) revert InvalidSignature();

        s.status = uint8(Status.Claimed);
        ILiquidityPool(pool).unlock(params.base.secretHash, params.base.token, params.base.amount);
        emit Claimed(params.base.secretHash, msg.sender, params.base.token, params.base.amount);
    }

    function _claimDestination(ClaimDestination calldata params) internal {
        Swap storage s = swaps[params.base.secretHash];

        _validateClaimDestination(params, s);

        address recipientAddress = params.base.user;

        s.status = uint8(Status.Claimed);
        ILiquidityPool(pool).transfer(
            params.base.secretHash,
            params.base.token,
            recipientAddress,
            params.base.amount
        );
        emit Claimed(
            params.base.secretHash,
            recipientAddress,
            params.base.token,
            params.base.amount
        );
    }

    function _refundSource(RefundSource calldata params) internal {
        Swap storage s = swaps[params.base.secretHash];

        _validateRefundSource(params, s);

        if (block.timestamp < s.lockUntil) revert LockNotExpired();

        s.status = uint8(Status.Refunded);
        ILiquidityPool(pool).transferForRefund(
            params.base.secretHash,
            params.base.token,
            params.base.user,
            params.base.amount
        );
        emit Refunded(params.base.secretHash);
    }

    function _refundDestination(Base calldata params) internal {
        Swap storage s = swaps[params.secretHash];

        _validateRefundDestination(params, s);
        if (block.timestamp < s.lockUntil) revert LockNotExpired();

        s.status = uint8(Status.Refunded);
        ILiquidityPool(pool).unlock(params.secretHash, params.token, params.amount);
        emit Refunded(params.secretHash);
    }

    function _hashParamsSource(
        bytes32 secretHash,
        uint256 amount,
        address token,
        address user,
        uint64 chainId,
        address sessionAddress
    ) internal pure returns (bytes27) {
        bytes32 hash = keccak256(
            abi.encode(secretHash, amount, token, user, chainId, sessionAddress)
        );
        return bytes27(uint216(uint256(hash)));
    }

    function _hashParamsDestination(
        bytes32 secretHash,
        uint256 amount,
        address token,
        address user,
        uint64 chainId
    ) internal pure returns (bytes27) {
        bytes32 hash = keccak256(abi.encode(secretHash, amount, token, user, chainId));
        return bytes27(uint216(uint256(hash)));
    }

    function _validateSecret(bytes32 secret, bytes32 secretHash) internal pure {
        if (keccak256(abi.encodePacked(secret)) != secretHash) {
            revert SecretMismatch();
        }
    }

    function _validateStatusSource(uint8 status) internal pure {
        if (status != uint8(Status.SourceLocked)) {
            revert WrongStatus();
        }
    }

    function _validateStatusDestination(uint8 status) internal pure {
        if (status != uint8(Status.DestinationLocked)) {
            revert WrongStatus();
        }
    }

    function _validateParamsSource(
        bytes32 secretHash,
        uint256 amount,
        address token,
        address user,
        uint64 chainId,
        address sessionAddress,
        bytes27 storedParamsHash
    ) internal pure {
        bytes27 computedHash = _hashParamsSource(
            secretHash,
            amount,
            token,
            user,
            chainId,
            sessionAddress
        );
        if (computedHash != storedParamsHash) revert ParamsMismatch();
    }

    function _validateParamsDestination(
        bytes32 secretHash,
        uint256 amount,
        address token,
        address user,
        uint64 chainId,
        bytes27 storedParamsHash
    ) internal pure {
        bytes27 computedHash = _hashParamsDestination(secretHash, amount, token, user, chainId);
        if (computedHash != storedParamsHash) revert ParamsMismatch();
    }

    function _validateHtlcLockSource(
        Base calldata core,
        address sessionAddress,
        uint256 nonce,
        uint64 maintainerDeadline,
        bytes calldata maintainerSig
    ) internal view {
        if (maintainerDeadline < block.timestamp) revert DeadlineExpired();
        if (block.chainid != core.chainId) revert InvalidStatus();

        bytes32 structHash = keccak256(
            abi.encode(
                HTLC_LOCK_TYPEHASH,
                core.secretHash,
                core.amount,
                core.token,
                core.user,
                sessionAddress,
                maintainerDeadline,
                nonce
            )
        );
        address signer = ECDSA.recover(_hashTypedDataV4(structHash), maintainerSig);
        if (signer != maintainer) revert InvalidSignature();
    }

    function _validateHtlcLockSourceWithFee(
        Base calldata core,
        address sessionAddress,
        uint256 nonce,
        uint64 maintainerDeadline,
        uint256 executionFeeNative,
        bytes calldata maintainerSig
    ) internal view {
        if (maintainerDeadline < block.timestamp) revert DeadlineExpired();
        if (block.chainid != core.chainId) revert InvalidStatus();

        bytes32 structHash = keccak256(
            abi.encode(
                HTLC_LOCK_WITH_FEE_TYPEHASH,
                core.secretHash,
                core.amount,
                core.token,
                core.user,
                sessionAddress,
                maintainerDeadline,
                nonce,
                executionFeeNative
            )
        );
        address signer = ECDSA.recover(_hashTypedDataV4(structHash), maintainerSig);
        if (signer != maintainer) revert InvalidSignature();
    }

    function _validateClaimSource(ClaimSource calldata params, Swap storage swap) internal view {
        _validateStatusSource(swap.status);

        _validateSecret(params.secret, params.base.secretHash);

        _validateParamsSource(
            params.base.secretHash,
            params.base.amount,
            params.base.token,
            params.base.user,
            params.base.chainId,
            params.sessionAddress,
            swap.paramsHash
        );
    }

    function _validateClaimDestination(
        ClaimDestination calldata params,
        Swap storage swap
    ) internal view {
        _validateStatusDestination(swap.status);

        _validateSecret(params.secret, params.base.secretHash);

        _validateParamsDestination(
            params.base.secretHash,
            params.base.amount,
            params.base.token,
            params.base.user,
            params.base.chainId,
            swap.paramsHash
        );
    }

    function _validateRefundSource(RefundSource calldata params, Swap storage swap) internal view {
        _validateStatusSource(swap.status);

        _validateParamsSource(
            params.base.secretHash,
            params.base.amount,
            params.base.token,
            params.base.user,
            params.base.chainId,
            params.sessionAddress,
            swap.paramsHash
        );
    }

    function _validateRefundDestination(Base calldata params, Swap storage swap) internal view {
        _validateStatusDestination(swap.status);

        _validateParamsDestination(
            params.secretHash,
            params.amount,
            params.token,
            params.user,
            params.chainId,
            swap.paramsHash
        );
    }

    function _authorizeUpgrade(
        address newImplementation
    ) internal override onlyRole(UPGRADER_ROLE) {}
}
