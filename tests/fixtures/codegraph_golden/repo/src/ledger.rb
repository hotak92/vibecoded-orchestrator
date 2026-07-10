# Ledger module for the golden-fixture repo (Ruby, regex-parsed).
# Exercises: module + class, inheritance, class reopening,
# instance methods, and module-level `def self.` methods.

require 'set'
require_relative 'helpers'

module Accounting
  def self.version
    '1.0'
  end
end

class Account
  def initialize(balance)
    @balance = balance
  end

  def deposit(amount)
    @balance += amount
  end

  def self.default
    new(0)
  end
end

# Class reopening: adds a method to the already-defined Account.
class Account
  def withdraw?(amount)
    amount <= @balance
  end
end

class SavingsAccount < Account
  def apply_interest(rate)
    deposit(@balance * rate)
  end
end
