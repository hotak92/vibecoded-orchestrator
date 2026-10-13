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

// v0.2.92 WP-5b: a BODILESS declaration. The method pattern ended in `{`, so
// no interface method and no abstract method anywhere produced a row.
interface Ledger {
    long balanceOf(String owner);
}

abstract class BaseAccount {
    abstract void audit();

    void touch() {
        // A statement whose captured "name" is a valid identifier. It becomes
        // matchable the moment `;` joins the terminator set, and only the
        // modifier/return-type run (`new`) tells it apart from a declaration.
        Object marker = new Object();
        this.audit();
    }
}
