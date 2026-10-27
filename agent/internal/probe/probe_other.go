//go:build !windows

package probe

func platformMemoryMiB() (int, error) { return 0, ErrUnsupported }
