// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20Token {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IVNDLToken {
    function mint(address to, uint256 amount) external returns (bool);
    function burn(address from, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/**
 * @title CryptoVNDLendingPool
 * @notice Dual-track lending market tailored for Vietnamese market.
 * - Supply Crypto (USDT/WBTC/WETH) -> Earn interest yield paid in VND / VNDL.
 * - Borrow VND / VNDL -> Lock Crypto Collateral without selling assets.
 */
contract CryptoVNDLendingPool {
    struct SupplyPosition {
        uint256 suppliedCrypto;
        uint256 lastAccrualTimestamp;
        uint256 accumulatedVNDLYield;
    }

    struct BorrowPosition {
        uint256 collateralCrypto;
        uint256 borrowedVNDL;
        uint256 lastBorrowTimestamp;
    }

    address public owner;
    IVNDLToken public vndl;

    // Supply APR in bps (e.g., 850 = 8.5% APY paid in VNDL)
    uint256 public supplyAprBps = 850;
    // Borrow APR in bps (e.g., 1050 = 10.5% APY)
    uint256 public borrowAprBps = 1050;

    // Token => User => Position
    mapping(address => mapping(address => SupplyPosition)) public supplies;
    mapping(address => mapping(address => BorrowPosition)) public borrows;

    event CryptoSupplied(address indexed token, address indexed user, uint256 amount);
    event YieldClaimed(address indexed token, address indexed user, uint256 vndlAmount);
    event CryptoWithdrawn(address indexed token, address indexed user, uint256 amount);
    event CollateralDeposited(address indexed token, address indexed user, uint256 amount);
    event VNDLBorrowed(address indexed token, address indexed user, uint256 amount);
    event VNDLRepaid(address indexed token, address indexed user, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "Unauthorized");
        _;
    }

    constructor(address _vndl) {
        owner = msg.sender;
        vndl = IVNDLToken(_vndl);
    }

    function setRates(uint256 _supplyApr, uint256 _borrowApr) external onlyOwner {
        supplyAprBps = _supplyApr;
        borrowAprBps = _borrowApr;
    }

    /**
     * @notice Deposit crypto to earn VNDL yield
     */
    function supplyCrypto(address token, uint256 amount) external {
        require(amount > 0, "Zero amount");
        SupplyPosition storage pos = supplies[token][msg.sender];

        // Accrue pending yield first
        _accrueYield(token, msg.sender);

        pos.suppliedCrypto += amount;
        pos.lastAccrualTimestamp = block.timestamp;

        require(IERC20Token(token).transferFrom(msg.sender, address(this), amount), "Supply transfer failed");
        emit CryptoSupplied(token, msg.sender, amount);
    }

    /**
     * @notice Claim accrued VNDL interest
     */
    function claimYield(address token) external {
        _accrueYield(token, msg.sender);
        SupplyPosition storage pos = supplies[token][msg.sender];
        uint256 yieldToClaim = pos.accumulatedVNDLYield;
        require(yieldToClaim > 0, "No yield available");

        pos.accumulatedVNDLYield = 0;
        require(vndl.mint(msg.sender, yieldToClaim), "VNDL yield mint failed");

        emit YieldClaimed(token, msg.sender, yieldToClaim);
    }

    /**
     * @notice Withdraw supplied crypto plus auto-claim yield
     */
    function withdrawCrypto(address token, uint256 amount) external {
        SupplyPosition storage pos = supplies[token][msg.sender];
        require(pos.suppliedCrypto >= amount, "Insufficient balance");

        _accrueYield(token, msg.sender);
        pos.suppliedCrypto -= amount;

        require(IERC20Token(token).transfer(msg.sender, amount), "Withdraw transfer failed");
        emit CryptoWithdrawn(token, msg.sender, amount);
    }

    function _accrueYield(address token, address user) internal {
        SupplyPosition storage pos = supplies[token][user];
        if (pos.suppliedCrypto == 0 || pos.lastAccrualTimestamp == 0) {
            pos.lastAccrualTimestamp = block.timestamp;
            return;
        }

        uint256 elapsed = block.timestamp - pos.lastAccrualTimestamp;
        if (elapsed > 0) {
            // Approximating yield in VNDL assuming oracle baseline (e.g. 1 USD ~ 25450 VND)
            // yield = (suppliedCrypto * 25450 * 1e18 / 1e6) * apr * elapsed / (365 days * 10000)
            uint256 principalVND = pos.suppliedCrypto * 25450 * 1e12; 
            uint256 newYield = (principalVND * supplyAprBps * elapsed) / (365 days * 10000);
            pos.accumulatedVNDLYield += newYield;
            pos.lastAccrualTimestamp = block.timestamp;
        }
    }
}
