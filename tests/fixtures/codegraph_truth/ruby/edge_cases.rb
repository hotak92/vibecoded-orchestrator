# Edge cases for the definitions gate (Ruby).
module Outer
  module Inner
    class Worker < Base
      def initialize(name)
        @name = name
      end

      def run! = perform(@name)

      def self.build(*args, **opts, &blk)
        new(*args)
      end

      class << self
        def configure
          yield self
        end
      end
    end
  end
end

class Outer::Inner::Worker
  def extra; end
end

LABEL = "def fake; end"
