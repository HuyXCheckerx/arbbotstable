// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title VNDL - Decentralized Vietnamese Dong Stablecoin
 * @notice 1 VNDL = 1 VND (Vietnamese Dong).
 * Minted strictly via over-collateralized vaults (VNDLVault) and Peg Stability Module (PSM).
 */
contract VNDL {
    string public constant name = "Vietnamese Dong Liquidity";
    string public constant symbol = "VNDL";
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    address public owner;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public isMinter;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event MinterUpdated(address indexed minter, bool status);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() {
        require(msg.sender == owner, "VNDL: caller is not the owner");
        _;
    }

    modifier onlyMinter() {
        require(isMinter[msg.sender], "VNDL: caller is not an authorized minter");
        _;
    }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "VNDL: new owner is zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function setMinter(address minter, bool status) external onlyOwner {
        require(minter != address(0), "VNDL: zero address minter");
        isMinter[minter] = status;
        emit MinterUpdated(minter, status);
    }

    function mint(address to, uint256 amount) external onlyMinter returns (bool) {
        require(to != address(0), "VNDL: mint to zero address");
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
        return true;
    }

    function burn(address from, uint256 amount) external onlyMinter returns (bool) {
        require(balanceOf[from] >= amount, "VNDL: burn amount exceeds balance");
        balanceOf[from] -= amount;
        totalSupply -= amount;
        emit Transfer(from, address(0), amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        return _transfer(msg.sender, to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 currentAllowance = allowance[from][msg.sender];
        if (currentAllowance != type(uint256).max) {
            require(currentAllowance >= amount, "VNDL: insufficient allowance");
            allowance[from][msg.sender] = currentAllowance - amount;
        }
        return _transfer(from, to, amount);
    }

    function _transfer(address from, address to, uint256 amount) internal returns (bool) {
        require(to != address(0), "VNDL: transfer to zero address");
        require(balanceOf[from] >= amount, "VNDL: transfer amount exceeds balance");

        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}
