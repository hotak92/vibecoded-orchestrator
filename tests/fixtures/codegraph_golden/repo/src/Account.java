// Account class for the golden-fixture repo (Java, regex-parsed).
package golden;

public class Account {
    private long balance;

    public Account(long opening) {
        this.balance = opening;
    }

    public void deposit(long amount) {
        this.balance += amount;
    }

    public long getBalance() {
        return this.balance;
    }
}
