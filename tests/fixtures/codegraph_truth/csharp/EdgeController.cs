// Edge cases for the definitions gate (C#).
using System;
using System.Threading.Tasks;

namespace Edge
{
    [Route("api/edge")]
    public sealed class EdgeController
    {
        public EdgeController() { }

        [HttpGet("one")]
        public async Task<int> GetOne() => await Task.FromResult(1);

        public string Name { get; set; }

        private static T Echo<T>(T value) where T : class
        {
            return value;
        }

        public struct Cursor
        {
            public int Advance(int by)
            {
                return by;
            }
        }
    }

    public interface IWorker<T>
    {
        void Work(T item);
    }

    public record Line(int A, int B)
    {
        public int Length() => B - A;
    }
}
