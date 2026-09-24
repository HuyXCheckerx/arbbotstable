// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IVNDL {
    function mint(address to, uint256 amount) external returns (bool);
    function burn(address from, uint256 amount) external returns (bool);
}

interface IPriceOracle {
    // Returns asset price in USD with 8 decimals (Chainlink standard)
    function getAssetPriceUSD(address asset) external view returns (uint256, uint256 updatedAt);
    // Returns USD/VND rate (e.g. 25450 * 10^8)
    function getUSDVNDRate() external view returns (uint256, uint256 updatedAt);
}

/**
 * @title VNDLVault
 * @notice Multi-collateral CDP Vault allowing users to deposit USDT and XAUt (Tether Gold)
 * to mint VNDL (Vietnamese Dong stablecoin).
 */
contract VNDLVault {
    struct CollateralConfig {
        bool isAllowed;
        uint8 decimals;
        uint256 mcrBps;              // Minimum Collateral Ratio in bps (e.g., 12000 = 120%)
        uint256 liquidationRatioBps; // Liquidation Ratio in bps (e.g., 11000 = 110%)
        uint256 liquidationBonusBps; // Liquidator bonus (e.g., 650 = 6.5%)
        uint256 totalDeposited;
    }

    struct Position {
        uint256 collateralAmount;
        uint256 debtVNDL;            // Total minted VNDL debt
        uint256 lastUpdated;
    }

    IVNDL public immutable vndl;
    IPriceOracle public oracle;
    address public owner;

    // Collateral address => config
    mapping(address => CollateralConfig) public collateralConfigs;
    // Collateral address => User => Position
    mapping(address => mapping(address => Position)) public positions;

    event CollateralDeposited(address indexed asset, address indexed user, uint256 amount);
    event CollateralWithdrawn(address indexed asset, address indexed user, uint256 amount);
    event VNDLMinted(address indexed asset, address indexed user, uint256 amount);
    event VNDLRepaid(address indexed asset, address indexed user, uint256 amount);
    event PositionLiquidated(
        address indexed asset,
        address indexed borrower,
        address indexed liquidator,
        uint256 debtRepaid,
        uint256 collateralSeized
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "VNDLVault: unauthorized");
        _;
    }

    constructor(address _vndl, address _oracle) {
        require(_vndl != address(0) && _oracle != address(0), "Zero address");
        vndl = IVNDL(_vndl);
        oracle = IPriceOracle(_oracle);
        owner = msg.sender;
    }

    function configureCollateral(
        address asset,
        uint8 decimals,
        uint256 mcrBps,
        uint256 liquidationRatioBps,
        uint256 liquidationBonusBps
    ) external onlyOwner {
        require(asset != address(0), "Invalid asset");
        require(mcrBps > liquidationRatioBps, "MCR must exceed liquidation threshold");

        collateralConfigs[asset] = CollateralConfig({
            isAllowed: true,
            decimals: decimals,
            mcrBps: mcrBps,
            liquidationRatioBps: liquidationRatioBps,
            liquidationBonusBps: liquidationBonusBps,
            totalDeposited: collateralConfigs[asset].totalDeposited
        });
    }

    /**
     * @notice Deposit collateral into the vault
     */
    function depositCollateral(address asset, uint256 amount) external {
        CollateralConfig storage cfg = collateralConfigs[asset];
        require(cfg.isAllowed, "Asset not supported");
        require(amount > 0, "Zero amount");

        require(IERC20(asset).transferFrom(msg.sender, address(this), amount), "Transfer failed");

        Position storage pos = positions[asset][msg.sender];
        pos.collateralAmount += amount;
        pos.lastUpdated = block.timestamp;
        cfg.totalDeposited += amount;

        emit CollateralDeposited(asset, msg.sender, amount);
    }

    /**
     * @notice Mint VNDL against deposited collateral
     */
    function mintVNDL(address asset, uint256 amountVNDL) external {
        CollateralConfig storage cfg = collateralConfigs[asset];
        require(cfg.isAllowed, "Asset not supported");
        require(amountVNDL > 0, "Zero mint amount");

        Position storage pos = positions[asset][msg.sender];
        uint256 newDebt = pos.debtVNDL + amountVNDL;

        uint256 colValVND = getCollateralValueInVND(asset, pos.collateralAmount);
        // Required collateral = newDebt * MCR / 10000
        uint256 requiredColVND = (newDebt * cfg.mcrBps) / 10000;
        require(colValVND >= requiredColVND, "VNDLVault: Exceeds maximum borrowing capacity");

        pos.debtVNDL = newDebt;
        pos.lastUpdated = block.timestamp;

        require(vndl.mint(msg.sender, amountVNDL), "Mint failed");
        emit VNDLMinted(asset, msg.sender, amountVNDL);
    }

    /**
     * @notice Repay VNDL debt to unlock collateral
     */
    function repayVNDL(address asset, uint256 amountVNDL) external {
        Position storage pos = positions[asset][msg.sender];
        require(pos.debtVNDL > 0, "No debt to repay");

        uint256 repayAmount = amountVNDL > pos.debtVNDL ? pos.debtVNDL : amountVNDL;
        pos.debtVNDL -= repayAmount;
        pos.lastUpdated = block.timestamp;

        require(vndl.burn(msg.sender, repayAmount), "Burn failed");
        emit VNDLRepaid(asset, msg.sender, repayAmount);
    }

    /**
     * @notice Withdraw excess collateral while maintaining MCR
     */
    function withdrawCollateral(address asset, uint256 amount) external {
        CollateralConfig storage cfg = collateralConfigs[asset];
        Position storage pos = positions[asset][msg.sender];
        require(pos.collateralAmount >= amount, "Insufficient collateral");

        uint256 remainingCol = pos.collateralAmount - amount;
        if (pos.debtVNDL > 0) {
            uint256 remainingValVND = getCollateralValueInVND(asset, remainingCol);
            uint256 requiredColVND = (pos.debtVNDL * cfg.mcrBps) / 10000;
            require(remainingValVND >= requiredColVND, "VNDLVault: Health factor below MCR");
        }

        pos.collateralAmount = remainingCol;
        pos.lastUpdated = block.timestamp;
        cfg.totalDeposited -= amount;

        require(IERC20(asset).transfer(msg.sender, amount), "Transfer failed");
        emit CollateralWithdrawn(asset, msg.sender, amount);
    }

    /**
     * @notice Liquidate under-collateralized vault
     */
    function liquidate(address asset, address borrower, uint256 debtToCover) external {
        CollateralConfig storage cfg = collateralConfigs[asset];
        Position storage pos = positions[asset][borrower];
        require(pos.debtVNDL > 0, "Position has no debt");

        uint256 colValVND = getCollateralValueInVND(asset, pos.collateralAmount);
        uint256 liqThresholdVND = (pos.debtVNDL * cfg.liquidationRatioBps) / 10000;
        require(colValVND < liqThresholdVND, "Position is healthy");

        uint256 maxRepay = pos.debtVNDL;
        uint256 actualRepay = debtToCover > maxRepay ? maxRepay : debtToCover;

        // Calculate collateral seized = (actualRepay * (10000 + bonus) / 10000) converted to asset units
        uint256 collateralVNDToSeize = (actualRepay * (10000 + cfg.liquidationBonusBps)) / 10000;
        uint256 collateralUnitsToSeize = convertVNDToAssetUnits(asset, collateralVNDToSeize);

        if (collateralUnitsToSeize > pos.collateralAmount) {
            collateralUnitsToSeize = pos.collateralAmount;
        }

        pos.debtVNDL -= actualRepay;
        pos.collateralAmount -= collateralUnitsToSeize;
        cfg.totalDeposited -= collateralUnitsToSeize;

        require(vndl.burn(msg.sender, actualRepay), "Burn from liquidator failed");
        require(IERC20(asset).transfer(msg.sender, collateralUnitsToSeize), "Collateral transfer failed");

        emit PositionLiquidated(asset, borrower, msg.sender, actualRepay, collateralUnitsToSeize);
    }

    /**
     * @notice Returns total collateral value evaluated in VND
     */
    function getCollateralValueInVND(address asset, uint256 amount) public view returns (uint256) {
        if (amount == 0) return 0;
        CollateralConfig storage cfg = collateralConfigs[asset];
        (uint256 priceUSD, ) = oracle.getAssetPriceUSD(asset);
        (uint256 usdVndRate, ) = oracle.getUSDVNDRate();

        // priceUSD has 8 decimals, usdVndRate has 8 decimals
        // normalized amount = amount * 10^(18 - cfg.decimals)
        // value in VND (18 decimals) = normalizedAmount * priceUSD / 1e8 * usdVndRate / 1e8
        uint256 normalizedAmount = amount * (10 ** (18 - cfg.decimals));
        uint256 valueUSD = (normalizedAmount * priceUSD) / 1e8;
        return (valueUSD * usdVndRate) / 1e8;
    }

    function convertVNDToAssetUnits(address asset, uint256 amountVND) public view returns (uint256) {
        CollateralConfig storage cfg = collateralConfigs[asset];
        (uint256 priceUSD, ) = oracle.getAssetPriceUSD(asset);
        (uint256 usdVndRate, ) = oracle.getUSDVNDRate();

        // valueUSD = amountVND * 1e8 / usdVndRate
        // units = valueUSD * 1e8 / priceUSD / 10^(18 - decimals)
        uint256 valueUSD = (amountVND * 1e8) / usdVndRate;
        uint256 normalizedUnits = (valueUSD * 1e8) / priceUSD;
        return normalizedUnits / (10 ** (18 - cfg.decimals));
    }
}
