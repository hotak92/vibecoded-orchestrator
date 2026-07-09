// Package golden is a Go fixture for the code-graph golden corpus.
package golden

import "fmt"

// Greeter holds a greeting prefix.
type Greeter struct {
	Prefix string
}

// Greet returns a greeting for the given name.
func (g *Greeter) Greet(name string) string {
	return fmt.Sprintf("%s %s", g.Prefix, name)
}

// NewGreeter builds a Greeter with the default prefix.
func NewGreeter() *Greeter {
	return &Greeter{Prefix: "hello"}
}

func addNumbers(a int, b int) int {
	return a + b
}
