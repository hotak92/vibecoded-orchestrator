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

# v0.2.92 WP-5b shapes. Every one of these was invisible or mis-measured
# before the `end`-keyword block scanner landed.
class Vault
  # Statement-MODIFIER `if`: takes no `end`. A scanner that counts it as a
  # block opener runs this body on to the next stray `end`.
  def store(amount)
    return 0 if amount.nil?
    @total = (@total || 0) + amount
  end

  # Ruby 3.0 endless method: no `end` at all, so the body is this one line.
  def total = @total
end

module Reporting
  # Indented class. The class pattern used to be anchored at column 0, so a
  # class nested in a module — the commonest Ruby file shape there is — was
  # absent from the graph entirely.
  class Summary
    def render(rows)
      rows.map { |r| r.to_s }.join(", ") unless rows.empty?
    end
  end
end

# Top-level method, declared AFTER every class has closed. Attributing it by
# "nearest preceding declaration" put it inside Summary.
def audit(ledger)
  ledger.each do |entry|
    puts entry if entry
  end
end
