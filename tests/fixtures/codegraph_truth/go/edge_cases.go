// Edge cases for the definitions gate (Go).
package edge

import "fmt"

type (
	Pair struct {
		A, B int
	}
	Namer interface {
		Name() string
	}
)

type Box[T any] struct {
	value T
}

func (b *Box[T]) Get() T {
	return b.value
}

func Map[T, U any](xs []T, f func(T) U) []U {
	out := make([]U, 0, len(xs))
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}

func (p Pair) Sum() int { return p.A + p.B }

var describe = func(p Pair) string {
	return fmt.Sprintf("%d", p.Sum())
}

const doc = "func Fake() {}"
